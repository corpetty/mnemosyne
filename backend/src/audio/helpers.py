import os
import json
import wget
from omegaconf import OmegaConf
import nltk
import re

def create_config(output_dir):
    """Create NeMo diarization config"""
    DOMAIN_TYPE = "telephonic"
    CONFIG_FILE_NAME = f"diar_infer_{DOMAIN_TYPE}.yaml"
    CONFIG_URL = f"https://raw.githubusercontent.com/NVIDIA/NeMo/main/examples/speaker_tasks/diarization/conf/inference/{CONFIG_FILE_NAME}"
    MODEL_CONFIG = os.path.join(output_dir, CONFIG_FILE_NAME)
    
    if not os.path.exists(MODEL_CONFIG):
        MODEL_CONFIG = wget.download(CONFIG_URL, output_dir)

    config = OmegaConf.load(MODEL_CONFIG)

    data_dir = os.path.join(output_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    meta = {
        "audio_filepath": os.path.join(output_dir, "mono_file.wav"),
        "offset": 0,
        "duration": None,
        "label": "infer",
        "text": "-",
        "rttm_filepath": None,
        "uem_filepath": None,
    }
    with open(os.path.join(data_dir, "input_manifest.json"), "w") as fp:
        json.dump(meta, fp)
        fp.write("\n")

    pretrained_vad = "vad_multilingual_marblenet"
    pretrained_speaker_model = "titanet_large"
    config.num_workers = 0
    config.diarizer.manifest_filepath = os.path.join(data_dir, "input_manifest.json")
    config.diarizer.out_dir = output_dir

    config.diarizer.speaker_embeddings.model_path = pretrained_speaker_model
    config.diarizer.oracle_vad = False
    config.diarizer.clustering.parameters.oracle_num_speakers = False

    config.diarizer.vad.model_path = pretrained_vad
    config.diarizer.vad.parameters.onset = 0.8
    config.diarizer.vad.parameters.offset = 0.6
    config.diarizer.vad.parameters.pad_offset = -0.05
    config.diarizer.msdd_model.model_path = "diar_msdd_telephonic"

    return config

def get_word_ts_anchor(s, e, option="start"):
    """Get word timestamp anchor point"""
    if option == "end":
        return e
    elif option == "mid":
        return (s + e) / 2
    return s

def get_words_speaker_mapping(wrd_ts, spk_ts, word_anchor_option="start"):
    """Map words to speakers based on timestamps"""
    s, e, sp = spk_ts[0]
    wrd_pos, turn_idx = 0, 0
    wrd_spk_mapping = []
    
    for wrd_dict in wrd_ts:
        ws, we, wrd = (
            int(wrd_dict["start"] * 1000),
            int(wrd_dict["end"] * 1000),
            wrd_dict["text"],
        )
        wrd_pos = get_word_ts_anchor(ws, we, word_anchor_option)
        
        while wrd_pos > float(e):
            turn_idx += 1
            turn_idx = min(turn_idx, len(spk_ts) - 1)
            s, e, sp = spk_ts[turn_idx]
            if turn_idx == len(spk_ts) - 1:
                e = get_word_ts_anchor(ws, we, option="end")
                
        wrd_spk_mapping.append({
            "word": wrd,
            "start_time": ws,
            "end_time": we,
            "speaker": sp
        })
    
    return wrd_spk_mapping

def get_sentences_speaker_mapping(word_speaker_mapping, spk_ts):
    """Group words into sentences with speaker labels"""
    sentence_checker = nltk.tokenize.PunktSentenceTokenizer().text_contains_sentbreak
    s, e, spk = spk_ts[0]
    prev_spk = spk

    sentences = []
    current_sentence = {
        "speaker": f"Speaker {spk}",
        "start_time": s,
        "end_time": e,
        "text": ""
    }

    for word_dict in word_speaker_mapping:
        word, spk = word_dict["word"], word_dict["speaker"]
        s, e = word_dict["start_time"], word_dict["end_time"]
        
        if spk != prev_spk or sentence_checker(current_sentence["text"] + " " + word):
            sentences.append(current_sentence)
            current_sentence = {
                "speaker": f"Speaker {spk}",
                "start_time": s,
                "end_time": e,
                "text": ""
            }
        else:
            current_sentence["end_time"] = e
            
        current_sentence["text"] += word + " "
        prev_spk = spk

    sentences.append(current_sentence)
    return sentences

def find_numeral_symbol_tokens(tokenizer):
    """Find tokens containing numerals or symbols"""
    numeral_symbol_tokens = [-1]
    for token, token_id in tokenizer.get_vocab().items():
        if any(c in "0123456789%$£" for c in token):
            numeral_symbol_tokens.append(token_id)
    return numeral_symbol_tokens

def convert_audio_format(input_file, output_file=None, format="opus", bitrate="64k"):
    """
    Convert audio from one format to another using pydub.
    
    Args:
        input_file: Path to the input audio file
        output_file: Path to save the output file (if None, will replace extension of input_file)
        format: Target format (opus, mp3, etc.)
        bitrate: Bitrate for compression (default: 64k - good for speech)
        
    Returns:
        Path to the converted file
    """
    try:
        from pydub import AudioSegment
        import os
        import subprocess
        
        # Check that ffmpeg is available (required by pydub)
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("Warning: ffmpeg not found or not working. Audio conversion may fail.")
            print("Please ensure ffmpeg is installed and in your PATH.")
        
        # Determine output path if not provided
        if output_file is None:
            base, _ = os.path.splitext(input_file)
            output_file = f"{base}.{format}"
        
        print(f"Converting {input_file} to {format} format at {output_file}")
        
        # Verify input file exists
        if not os.path.exists(input_file):
            raise FileNotFoundError(f"Input file not found: {input_file}")
        
        # Create output directory if it doesn't exist
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        
        # Load audio file with appropriate parameters
        try:
            audio = AudioSegment.from_file(input_file)
        except Exception as e:
            print(f"Error loading audio file: {e}")
            # Try with explicit format
            source_format = os.path.splitext(input_file)[1].lower()[1:]
            if source_format:
                print(f"Retrying with explicit format: {source_format}")
                audio = AudioSegment.from_file(input_file, format=source_format)
            else:
                raise
        
        # For OPUS, we ensure mono and appropriate sample rate for speech
        if format.lower() == "opus":
            # Convert to mono if stereo
            if audio.channels > 1:
                print(f"Converting {audio.channels} channels to mono")
                audio = audio.set_channels(1)
            
            # 16kHz is good for speech
            if audio.frame_rate != 16000:
                print(f"Converting sample rate from {audio.frame_rate}Hz to 16000Hz")
                audio = audio.set_frame_rate(16000)
        
        # Export parameters for different formats
        export_params = {}
        if format.lower() == "opus":
            export_params = {
                "format": format,
                "bitrate": bitrate,
                "parameters": ["-application", "voip"]  # Optimize for speech
            }
        else:
            export_params = {
                "format": format,
                "bitrate": bitrate
            }
        
        # Export to the target format
        audio.export(output_file, **export_params)
        
        # Verify the output file exists and has content
        if not os.path.exists(output_file):
            raise FileNotFoundError(f"Output file was not created: {output_file}")
        
        if os.path.getsize(output_file) == 0:
            raise ValueError(f"Output file is empty: {output_file}")
            
        print(f"Successfully converted audio file to {format} format")
        return output_file
        
    except Exception as e:
        print(f"Error in convert_audio_format: {e}")
        import traceback
        print(f"Error traceback: {traceback.format_exc()}")
        raise
