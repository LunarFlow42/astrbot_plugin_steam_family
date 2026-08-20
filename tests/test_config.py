from config import PluginConfig

def test_default_config():
    config = PluginConfig()
    assert config.poll_interval_minutes == 15
    assert config.enable_render_card is True
    assert config.filter_non_games is True
    assert config.subscribers == []
    assert config.access_token == ""

def test_custom_config():
    config = PluginConfig(
        access_token="test_token_123",
        steam_id="76561198000000000",
        poll_interval_minutes=30,
        subscribers=["group_1001", "private_2002"]
    )
    assert config.access_token == "test_token_123"
    assert config.steam_id == "76561198000000000"
    assert config.poll_interval_minutes == 30
    assert len(config.subscribers) == 2
