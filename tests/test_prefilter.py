from routes._prefilter import _classify, apply_noise_prefilter


def test_low_convergence_project_follow_is_kept():
    text = """[base] Alice 关注了 TinyAgents
用户简介:AI agent protocol launching soon on Base. Onchain app for autonomous trading.
你关注的1个用户也关注了ta
https://x.com/tinyagents"""

    assert _classify(text) == ("keep", "follow+project")


def test_tool_follow_is_dropped_even_with_convergence():
    text = """[國人] Alice 关注了 Hyperdash
用户简介:Trade Stocks, Oil, Crypto and FX 24/7.

Track, analyze and follow the top traders globally with institutional grade execution and data analytics.
你关注的13个用户也关注了ta
https://x.com/hypurrdash"""

    assert _classify(text) == ("drop", "follow-tool-account")


def test_priority_tag_does_not_bypass_personal_follow_filter():
    text = """[alpha] 0xVaidhik 关注了 Prof. Jrii
用户简介:no flexing profit, tuning for @Porsche | Loyalist @ManUtd
Hit follow & be part of the story!
你关注的1个用户也关注了ta
https://x.com/ProfJrii"""

    assert _classify(text) == ("drop", "follow-personal-account")


def test_source_kol_name_does_not_make_target_look_like_project():
    text = """[alpha] OrdinalsNFT 关注了 Shweep
用户简介:baaaaaa.
i forgot what i was doing.
你关注的3个用户也关注了ta
https://x.com/iamshweep"""

    assert _classify(text) == ("drop", "follow-noise")


def test_priority_tag_still_keeps_non_follow_events():
    text = """[alpha] GE 发布新推文
Early Project

@xergHQ - 20 Followers
Category : AI"""

    assert _classify(text) == ("keep", "priority[alpha]")


def test_apply_noise_prefilter_only_targets_noisy_chat():
    messages = [
        {
            "text": """[alpha] Alice 关注了 PersonalAcct
用户简介:Founder, investor and writer. Opinions are my own.
你关注的1个用户也关注了ta
https://x.com/personal"""
        },
        {
            "text": """[base] Alice 关注了 NewMint
用户简介:NFT freemint collection launching soon on Ethereum.
你关注的1个用户也关注了ta
https://x.com/newmint"""
        },
    ]

    filtered, stats = apply_noise_prefilter(messages, "2423905766")
    assert len(filtered) == 1
    assert "NewMint" in filtered[0]["text"]
    assert stats["dropped"] == 1

    untouched, stats = apply_noise_prefilter(messages, "other")
    assert untouched == messages
    assert stats is None
