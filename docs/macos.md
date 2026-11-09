# Mnemosyne on a Mac

Test builds for Apple silicon. The first half is for someone trying a build; the second is for the
maintainer, about the signing certificate. The plan and its reasons are in
[plans/2026-11-09-macos.md](plans/2026-11-09-macos.md).

## Trying a test build

### Requirements

- A Mac with Apple silicon (M1 or later). Intel Macs are not built.
- macOS 15.4 or later. Mnemosyne plays meeting audio as Ogg, which the system web view only reads
  from 15.4.
- About 10 GB of free disk for the engine and models, and an internet connection on first launch.

### Where builds come from

For now a build is the `mnemosyne-macos-app` artifact of a "Smoke test (macOS)" workflow run on
GitHub Actions: a zip, kept 7 days. You are sent the link. Later builds will be a download on the
release page.

### Install

1. Unzip it. You get `Mnemosyne.app`.
2. Drag `Mnemosyne.app` to Applications **before** opening it. Opened from Downloads, macOS runs it
   from a temporary location and permissions do not stick.
3. Open it from Applications.

macOS will probably refuse ("Apple could not verify..."), because the build is not notarized. Either:

- System Settings → Privacy & Security, scroll down, **Open Anyway**, and confirm. (Since macOS
  Sequoia, right-click → Open no longer works.)
- Or in Terminal: `xattr -dr com.apple.quarantine /Applications/Mnemosyne.app`

The first launch installs the engine (Python and its packages) for a few minutes. Let it finish, then
go through the setup.

### Permissions

| Permission | When | Why |
|---|---|---|
| Microphone | when recording starts | your voice |
| System Audio Recording | when recording starts | the other people on a call |
| Local Network | on first launch | finding your other computers |

Change any of them in System Settings → Privacy & Security (Microphone; Screen & System Audio
Recording, the "System Audio Recording Only" list; Local Network). If the system audio level stays
flat while something plays, look there first.

### What works in this build

Be aware this is a test build.

- If recording is not in your build yet, you can import recordings (Import, or the sample meeting)
  and transcribe them. Your build's notes say which it is.
- Transcription runs on the CPU with Parakeet, and speaker labels with the onnx diarizer. It is
  slower than on a PC with a GPU.
- Not on the Mac yet: Phonon-2 and the built-in summary model. Use Ollama, vLLM, OpenAI or
  Anthropic for summaries, or a remote main computer.
- Linux only: echo cancellation and noise suppression. Use headphones for calls when you can.

### Checklist

Do these in order and write down what happened, especially what surprised you.

1. Install as above. Did anything need more than the steps say?
2. Finish the setup. How long did the first launch take?
3. Allow the microphone and System Audio Recording when asked.
4. Import the sample meeting. Does it play, and does it transcribe with several speakers?
5. Hold a 30-minute call (Zoom, Meet, whatever you use) with someone, once on headphones and once on
   the laptop speakers. Watch the live transcript, then Stop and transcribe. Are both sides there,
   with speakers?
6. During a recording, connect AirPods (or other Bluetooth headphones). Does recording carry on?
7. During a recording, close the lid for a minute and open it. What happened?
8. During a recording, Force Quit Mnemosyne (Option-Command-Esc), then reopen it within 15 minutes.
   Is the recording kept?
9. Accept the contact invite you were sent, and open a meeting shared with you. Check that you see
   only what was shared, and that the sender sees only what you sent back.
10. When an update is offered, install it. Did macOS ask for any permission again? Does recording
    still work?

### Reporting

After any problem: Settings → General → Troubleshooting → **Copy diagnostics**, then paste it into a
message. It holds versions, your Mac and how the app is installed, the engines, recent errors, your
settings (keys, names and addresses left out) and the end of the log. Read it first: the log can
mention meeting names. Say what you did and what you expected.

## For the maintainer: the signing certificate

macOS ties a permission grant to the app's signing certificate. An ad-hoc signature is different for
every build, so a tester would have to grant the microphone and system audio again after each update.
A stable self-signed certificate keeps the grants across updates. It is free and is not a trust
signal: macOS still blocks the first open.

Create it once, on any machine with openssl:

```
openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 3650 -nodes \
  -subj "/CN=Mnemosyne Test Signing" \
  -addext "keyUsage=critical,digitalSignature" -addext "extendedKeyUsage=critical,codeSigning"
openssl pkcs12 -export -inkey key.pem -in cert.pem -name "Mnemosyne Test Signing" \
  -certpbe PBE-SHA1-3DES -keypbe PBE-SHA1-3DES -macalg sha1 -out mnemosyne-macos-signing.p12
base64 -w0 mnemosyne-macos-signing.p12 | gh secret set APPLE_CERTIFICATE
gh secret set APPLE_CERTIFICATE_PASSWORD   # the export password you typed
```

The 3DES and SHA1 options matter: macOS's keychain import does not read OpenSSL 3's default `.p12`
encryption.

Keep the `.p12` and its password with the updater key (`~/.tauri/`) and delete `key.pem` and
`cert.pem`. Losing the certificate is mild, unlike the updater key: testers grant permissions once
more after the next update.

Without the secrets, CI signs with a throwaway certificate on each run, so grants do not survive an
update.

Later, with an Apple Developer account, a Developer ID certificate and notarization replace this.
Changing the identity costs testers one more round of permission prompts.
