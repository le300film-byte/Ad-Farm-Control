"""Guild provisioning: full server layout, permissions, idempotency, id persistence."""
from adfarm.config import Settings
from adfarm.db import Database
from adfarm.discord.provision import (ADMIN_ROLE_NAME, ALL_CHANNELS, HUB_CATEGORY_NAME, HUB_META_KEY, PUBLIC_CHANNELS, STAFF_CHANNELS,
                                      GuildProvisioner, staff_overwrites)
from adfarm.services.container import Repos
from tests.conftest import ADMIN, ADMIN2, run
from tests.fakes import FakeGuildAdmin


def _prov(api, **kw):
    return GuildProvisioner(api, owner_ids=(ADMIN, ADMIN2), **kw)


def test_creates_every_channel_category_and_role():
    api = FakeGuildAdmin()
    report = run(_prov(api).provision())
    assert report.ok, report.failures
    for spec in ALL_CHANNELS:
        assert spec.name in api.channels
    assert HUB_CATEGORY_NAME in api.categories
    assert ADMIN_ROLE_NAME in api.roles
    # both owners were given the Bot Admin role
    assert sorted(api.role_members[api.roles[ADMIN_ROLE_NAME]]) == sorted([ADMIN, ADMIN2])
    assert report.ids[HUB_META_KEY] == api.categories[HUB_CATEGORY_NAME]
    assert report.ids["ADMIN_COMMANDS_CH_ID"] == api.channels["admin-commands"]
    assert report.ids["ADMIN_ALERTS_CH_ID"] == api.channels["admin-chat"]


def test_permissions_public_open_staff_hidden_hub_hidden():
    api = FakeGuildAdmin()
    run(_prov(api).provision())
    for spec in PUBLIC_CHANNELS:
        ow = {o.target: o for o in api.overwrites[api.channels[spec.name]]}
        assert "view_channel" in ow["everyone"].allow
        assert "send_messages" in ow["everyone"].allow
    for spec in STAFF_CHANNELS:
        ow = {o.target: o for o in api.overwrites[api.channels[spec.name]]}
        assert "view_channel" in ow["everyone"].deny
        members = [o.target_id for o in api.overwrites[api.channels[spec.name]] if o.target == "member"]
        assert sorted(members) == sorted([ADMIN, ADMIN2])
    hub = {o.target: o for o in api.overwrites[api.categories[HUB_CATEGORY_NAME]]}
    assert "view_channel" in hub["everyone"].deny


def test_idempotent_second_run_creates_nothing():
    api = FakeGuildAdmin()
    run(_prov(api).provision())
    before = dict(api.channels)
    second = run(_prov(api).provision())
    assert api.channels == before
    assert second.created == [] and len(second.reused) == len(ALL_CHANNELS) + 3


def test_existing_channel_is_reused_and_healed_not_duplicated():
    api = FakeGuildAdmin(existing_channels={"general-chat": "111", "audit-logs": "222"})
    report = run(_prov(api).provision())
    assert api.channels["general-chat"] == "111" and api.channels["audit-logs"] == "222"
    assert "general-chat" in report.reused and "audit-logs" in report.reused
    assert "view_channel" in {o.target: o for o in api.overwrites["222"]}["everyone"].deny
    assert api.parents["222"]  # re-parented under the staff category


def test_failures_are_collected_and_do_not_abort():
    api = FakeGuildAdmin(fail_on={"pricing-plans"})
    report = _prov(api)
    result = run(report.provision())
    assert not result.ok and result.failures[0][0] == "pricing-plans"
    assert "welcome-about" in api.channels and "audit-logs" in api.channels


def test_ids_are_persisted_to_meta_and_reloaded_into_settings(tmp_path):
    db = Database(str(tmp_path / "a.db"))
    db.migrate()
    repos = Repos.for_db(db)
    api = FakeGuildAdmin()
    run(GuildProvisioner(api, owner_ids=(ADMIN,), store=repos.meta.set).provision())
    stored = repos.meta.all()
    assert stored[HUB_META_KEY] == api.categories[HUB_CATEGORY_NAME]
    settings = Settings().with_channel_ids(stored)
    assert settings.customer_hub_category_id == api.categories[HUB_CATEGORY_NAME]
    assert settings.audit_log_channel_id == api.channels["audit-logs"]
    assert settings.ticket_channel_id == api.channels["open-ticket"]
    # explicit env config always wins over the stored ids
    assert Settings(customer_hub_category_id="env").with_channel_ids(stored).customer_hub_category_id == "env"


def test_dry_run_touches_nothing():
    api = FakeGuildAdmin()
    report = run(_prov(api, dry_run=True).provision())
    assert api.channels == {} and api.categories == {} and api.roles == {}
    assert HUB_CATEGORY_NAME in report.created


def test_provisioned_names_are_classified_correctly(settings):
    from adfarm.discord.channels import ChannelClassifier
    from adfarm.discord.ports import ChannelRef
    from adfarm.security.policy import ChannelKind

    c = ChannelClassifier(settings, lambda _id: None)
    kinds = {spec.name: c.classify(ChannelRef(id="x", name=spec.name, guild_id="1")).kind_hint for spec in ALL_CHANNELS}
    assert kinds["admin-commands"] is ChannelKind.ADMIN
    assert kinds["admin-chat"] is ChannelKind.ADMIN
    assert kinds["audit-logs"] is ChannelKind.ADMIN
    assert kinds["open-ticket"] is ChannelKind.TICKET
    for name in ("welcome-about", "pricing-plans", "whats-new", "general-chat"):
        assert kinds[name] is ChannelKind.PUBLIC


def test_staff_overwrites_helper_shape():
    ows = staff_overwrites("role1", ["7"])
    assert any(o.target == "role" and o.target_id == "role1" for o in ows)
    assert any(o.target == "bot" for o in ows)
