from __future__ import annotations

from buyerintel.config import load_settings, load_yaml


def test_settings_loads_all_config_files():
    settings = load_settings()
    assert settings.engines, "engines.yaml must be present"
    assert settings.providers, "providers.yaml must be present"
    assert settings.weights, "weights.yaml must be present"
    assert settings.personas, "personas.yaml must be present"
    assert settings.languages, "languages.yaml must be present"
    assert settings.channels, "channels.yaml must be present"
    assert settings.schedules is not None


def test_weights_sum_to_100():
    weights = load_yaml("weights.yaml")
    total = sum(weights["weights"].values())
    assert total == 100, f"scoring weights must total 100, got {total}"


def test_eight_personas_defined():
    personas = load_yaml("personas.yaml")["personas"]
    assert len(personas) == 8
    ids = [p["id"] for p in personas]
    assert ids == [f"P{i}" for i in range(1, 9)]


def test_secrets_never_loaded_from_committed_files():
    # load_secrets must tolerate a missing secrets.yaml (it is gitignored).
    from buyerintel.config import load_secrets

    secrets = load_secrets()
    assert isinstance(secrets, dict)


def test_language_seed_has_no_arabic_ui_conflict():
    # Arabic is a data language for search, not a UI language (spec section 2).
    languages = load_yaml("languages.yaml")
    assert languages["confirmed"] is False
    assert any(l["code"] == "ar" for l in languages["languages"])
