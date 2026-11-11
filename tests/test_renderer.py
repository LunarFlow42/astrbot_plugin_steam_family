from core.renderer import CardRenderer


def test_template_rendering():
    renderer = CardRenderer()
    data = {
        "appid": 2358720,
        "game_name": "黑神话：悟空",
        "original_name": "Black Myth: Wukong",
        "header_image": "https://cdn.akamai.steamstatic.com/steam/apps/2358720/header.jpg",
        "owner_name": "义父一号",
        "initial_price": "¥ 268.00",
        "final_price": "¥ 268.00",
        "discount_percent": 0,
        "review_summary": "好评如潮",
        "positive_rate": 96,
        "genres": ["动作", "角色扮演", "神话"],
    }
    html = renderer.render_html(data)
    assert "黑神话：悟空" in html
    assert "义父一号" in html
    assert "好评如潮" in html


def test_dashboard_template_rendering():
    renderer = CardRenderer()
    data = {
        "total_games": 50,
        "total_initial_value": 12580.0,
        "total_current_value": 9800.0,
        "member_count": 5,
        "rankings": [
            {
                "steamid": "76561198000000001",
                "game_count": 25,
                "total_initial_price": 6000.0,
                "total_current_price": 5000.0,
            }
        ],
        "recent_games": [
            {
                "appid": 1086940,
                "name": "博德之门3",
                "header_image": "https://...",
                "final_price": 298.0,
                "positive_rate": 96,
                "genres": ["RPG"],
            }
        ],
    }
    html = renderer.render_dashboard_html(data)
    assert "Steam 家庭资产全景看板" in html
    assert "12580" in html
    assert "博德之门3" in html


def test_autoescape_escapes_script_in_external_text():
    """(P2-13) autoescape：昵称/游戏名等外部文本中的 <script> 必须被转义，正常中文不受影响。"""
    renderer = CardRenderer()
    data = {
        "appid": 2358720,
        "game_name": "奇怪的游戏 <b>加粗</b>",
        "original_name": "Weird Game <script>alert(1)</script>",
        "header_image": "https://cdn.akamai.steamstatic.com/steam/apps/2358720/header.jpg",
        "owner_name": "<script>alert('xss')</script>",
        "initial_price": "¥ 268.00",
        "final_price": "¥ 268.00",
        "discount_percent": 0,
        "review_summary": "好评如潮 & 特别好评",
        "positive_rate": 96,
        "genres": ["动作", "角色扮演"],
    }
    html = renderer.render_html(data)

    # 原始 HTML 不得原样出现（防注入），统一转义为实体
    assert "<script>alert('xss')</script>" not in html
    assert "&lt;script&gt;" in html
    assert "奇怪的游戏 &lt;b&gt;加粗&lt;/b&gt;" in html
    # 连接符同样被转义（review_summary 为外部文本）
    assert "好评如潮 &amp; 特别好评" in html
