"""core: models, rules, clock, errors."""
import pytest

from adfarm.core import rules
from adfarm.core.clock import FakeClock
from adfarm.core.errors import ValidationError
from adfarm.core.models import DAY, Alt, Customer, Tier


def test_tier_ranking_and_cover():
    assert Tier.ADMIN.covers(Tier.VIP) and Tier.VIP.covers(Tier.CUSTOMER) and Tier.CUSTOMER.covers(Tier.PUBLIC)
    assert not Tier.CUSTOMER.covers(Tier.VIP)
    assert not Tier.PUBLIC.covers(Tier.CUSTOMER)


def test_customer_tier_depends_on_expiry_and_flags():
    now = 1000.0
    c = Customer("1", "u", 1, False, 0, now + DAY, True)
    assert c.tier(now) is Tier.CUSTOMER
    assert c.with_(vip=True).tier(now) is Tier.VIP
    assert c.with_(expiry_date=now - 1).tier(now) is Tier.PUBLIC
    assert c.with_(active=False).tier(now) is Tier.PUBLIC
    assert c.days_remaining(now) == pytest.approx(1.0)


def test_alt_label_and_slug():
    a = Alt("1", 2, 7, "worker1", "alice_alt2")
    assert a.repo_slug == "worker1/alice_alt2" and a.label == "Alt 2"
    assert a.with_(username="bob").label == "bob"
    assert a.with_(display_name="Main", username="bob").label == "Main"


def test_fake_clock():
    c = FakeClock(10)
    assert c.now() == 10 and c.advance(5) == 15 and c.now() == 15


@pytest.mark.parametrize("raw,expected", [("2.3", 2.3), ("$2.30/1k", 2.3), ("20", 20.0), ("0.01", 0.01)])
def test_validate_price_ok(raw, expected):
    assert rules.validate_price(raw) == expected


@pytest.mark.parametrize("raw", ["0", "20.01", "abc", "", "inf", "$0.00"])
def test_validate_price_rejects(raw):
    with pytest.raises(ValidationError):
        rules.validate_price(raw)


def test_validate_channel_ids_dedupes_and_caps():
    ids = rules.validate_channel_ids("111111111111111111, 222222222222222222,111111111111111111")
    assert ids == ("111111111111111111", "222222222222222222")
    with pytest.raises(ValidationError):
        rules.validate_channel_ids(",".join(str(100000000000000000 + i) for i in range(11)))
    with pytest.raises(ValidationError):
        rules.validate_channel_ids("not-an-id")
    with pytest.raises(ValidationError):
        rules.validate_channel_ids("")


def test_validate_interval_runtime_mode_policy():
    assert rules.validate_interval("3") == 3 and rules.validate_interval(5) == 5
    with pytest.raises(ValidationError):
        rules.validate_interval(4)
    assert rules.validate_runtime(0) == 0 and rules.validate_runtime("48") == 48
    with pytest.raises(ValidationError):
        rules.validate_runtime(7)
    assert rules.validate_ad_type(" SELL ") == "sell"
    with pytest.raises(ValidationError):
        rules.validate_ad_type("trade")
    assert rules.validate_policy("Stealth") == "stealth"
    assert rules.policy_defaults("aggressive")["interval_min"] == 3


def test_validate_message_and_autoreply():
    with pytest.raises(ValidationError):
        rules.validate_message("")
    with pytest.raises(ValidationError):
        rules.validate_message("x" * 1901)
    assert rules.validate_autoreply("hi @everyone") == "hi (mention:everyone)"


def test_validate_keywords():
    assert rules.validate_keywords("Skins, skins ,  gems") == ("Skins", "gems")
    with pytest.raises(ValidationError):
        rules.validate_keywords("")
    with pytest.raises(ValidationError):
        rules.validate_keywords(",".join(f"k{i}" for i in range(21)))


def test_validate_days_alt_count_alt_index_confirmation():
    assert rules.validate_days("30") == 30
    with pytest.raises(ValidationError):
        rules.validate_days(0)
    with pytest.raises(ValidationError):
        rules.validate_days(367)
    assert rules.validate_alt_count(4) == 4
    with pytest.raises(ValidationError):
        rules.validate_alt_count(5)
    assert rules.validate_alt_index("2", 2) == 2
    with pytest.raises(ValidationError):
        rules.validate_alt_index(3, 2)
    rules.validate_confirmation(" reset ", "RESET")
    with pytest.raises(ValidationError):
        rules.validate_confirmation("nope", "RESET")


def test_repo_name_sanitised():
    assert rules.repo_name_for("Alice Smith!", 1) == "alice_smith_alt1"
    assert rules.repo_name_for("", 2) == "customer_alt2"
