"""The machine probe used by the setup wizard."""


def test_system_info(client, ctx, monkeypatch):
    from mnemosyne.api.routes import system

    monkeypatch.setattr(
        system.shutil, "which", lambda b: "/usr/bin/" + b if b != "nvidia-smi" else None
    )
    monkeypatch.setattr(system, "_has", lambda m: m == "onnx_asr")

    def no_cuda(name):
        raise OSError("not found")

    monkeypatch.setattr(system.ctypes, "CDLL", no_cuda)
    info = client.get("/api/system").json()
    assert info["gpu_driver"] is False and info["gpu_stack"] is False
    assert info["parakeet"] is True and info["pipewire"] is True and info["ffmpeg"] is True
    assert info["hf_token"] is False
    ctx.settings.hf_token = "hf_x"
    assert client.get("/api/system").json()["hf_token"] is True


def test_setup_complete_is_a_setting(client):
    assert client.get("/api/settings").json()["values"]["setup_complete"] is False
    r = client.put("/api/settings", json={"setup_complete": True})
    assert r.json()["values"]["setup_complete"] is True
