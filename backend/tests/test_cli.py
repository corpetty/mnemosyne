"""Entry-point details."""

import warnings

from mnemosyne.cli import quiet_known_warnings


def test_torchcodec_warning_is_silenced():
    # pyannote's message starts with a newline (the old '.*torchcodec.*' never matched it).
    message = (
        "\ntorchcodec is not installed correctly so built-in audio decoding will fail. "
        "Solutions are: ..."
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        quiet_known_warnings()
        warnings.warn(message, UserWarning, stacklevel=1)
        warnings.warn(
            "TensorFloat-32 (TF32) has been disabled as it might lead to reproducibility issues",
            UserWarning,
            stacklevel=1,
        )
        warnings.warn(
            "std(): degrees of freedom is <= 0. Correction should be strictly less",
            UserWarning,
            stacklevel=1,
        )
        warnings.warn("something else", UserWarning, stacklevel=1)
    assert [str(w.message) for w in caught] == ["something else"]
