"""Mock data for testing Steam Family APIs."""

MOCK_FAMILY_APPS_RESPONSE = {
    "response": {
        "apps": [
            {
                "appid": 1086940,
                "name": "Baldur's Gate 3",
                "packageid": 393963,
                "owner_steamids": ["76561198000000001"],
                "exclude_reason": 0,
                "is_free": False,
            },
            {
                "appid": 1245620,
                "name": "ELDEN RING",
                "packageid": 456123,
                "owner_steamids": ["76561198000000002"],
                "exclude_reason": 0,
                "is_free": False,
            },
        ]
    }
}

MOCK_APP_DETAILS_RESPONSE = {
    "1086940": {
        "success": True,
        "data": {
            "type": "game",
            "name": "Baldur's Gate 3",
            "steam_appid": 1086940,
            "is_free": False,
            "short_description": "召集你的队伍，返回被遗忘的国度...",
            "header_image": "https://cdn.akamai.steamstatic.com/steam/apps/1086940/header.jpg",
            "price_overview": {
                "currency": "CNY",
                "initial": 29800,
                "final": 29800,
                "discount_percent": 0,
            },
            "genres": [
                {"id": "3", "description": "角色扮演"},
                {"id": "2", "description": "策略"},
            ],
            "categories": [
                {"id": 1, "description": "多玩家"},
                {"id": 9, "description": "合作"},
            ],
        },
    }
}
