from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_sgi_dry_preserves_stub_signature_contract() -> None:
    prompt = (ROOT / "benchmarks" / "SGI-DryExperiment" / "role_prompt.md").read_text(encoding="utf-8")

    assert "Function interface contract" in prompt
    assert "fixed" in prompt and "interface contract" in prompt
    assert "Do not infer new parameters from the research direction" in prompt
    assert "original incomplete stub signature exactly" in prompt
    assert "Record or check the original incomplete function signatures before editing" in prompt
    assert "Allowed edit scope" in prompt
    assert "data_en.py" in prompt and "read-only" in prompt
    assert "Only fill the missing function bodies in `main_en.py`" in prompt
    assert "Do not change imports, constants, helper functions, call sites" in prompt
