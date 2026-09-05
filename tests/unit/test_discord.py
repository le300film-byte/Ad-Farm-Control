"""discord: channel classifier, forum provisioner (webhooks!), embeds, replies."""
from adfarm.core.models import DAY, Alt, Customer, Tier
from adfarm.discord import ChannelClassifier, ForumProvisioner, Reply, THREADS, VIP_THREADS, account_embed, alt_status_embed, help_embed
from adfarm.discord.ports import ChannelRef
from adfarm.security.policy import ChannelKind
from tests.conftest import ADMIN_CH, CUSTOMER, HUB_CATEGORY, PUBLIC_CH, TICKET_CH, run
from tests.fakes import FakeDiscord


def _classifier(settings, forum_owner=None):
    table = {"f1": Customer(CUSTOMER, "alice", 1, False, 0, 10**10, True, forum_id="f1")} if forum_owner is None else forum_owner
    return ChannelClassifier(settings, table.get)


def test_classifier_by_ids_names_and_category(settings):
    c = _classifier(settings)
    assert c.classify(None).kind_hint is ChannelKind.DM
    assert c.classify(ChannelRef(id="9", kind="dm")).kind_hint is ChannelKind.DM
    assert c.classify(ChannelRef(id=ADMIN_CH, name="whatever", guild_id="1")).kind_hint is ChannelKind.ADMIN
    assert c.classify(ChannelRef(id="x", name="admin-chat", guild_id="1")).kind_hint is ChannelKind.ADMIN
    assert c.classify(ChannelRef(id="x", name="random", category_name="Admin Zone", guild_id="1")).kind_hint is ChannelKind.ADMIN
    assert c.classify(ChannelRef(id=TICKET_CH, name="zzz", guild_id="1")).kind_hint is ChannelKind.TICKET
    assert c.classify(ChannelRef(id="x", name="open-ticket", guild_id="1")).kind_hint is ChannelKind.TICKET
    assert c.classify(ChannelRef(id=PUBLIC_CH, name="general-chat", guild_id="1")).kind_hint is ChannelKind.PUBLIC
    hub = c.classify(ChannelRef(id="t1", name="control", kind="thread", parent_id="f1", guild_id="1"))
    assert hub.kind_hint is ChannelKind.OWN_HUB and hub.hub_owner_id == CUSTOMER
    forum = c.classify(ChannelRef(id="f1", name="alice-hub", kind="forum", guild_id="1"))
    assert forum.hub_owner_id == CUSTOMER
    orphan = c.classify(ChannelRef(id="t9", name="control", kind="thread", parent_id="f9", category_id=HUB_CATEGORY, guild_id="1"))
    assert orphan.kind_hint is ChannelKind.OTHER_HUB
    marker = c.classify(ChannelRef(id="t8", name="x", category_name="🏢 Customer Hub", guild_id="1"))
    assert marker.kind_hint is ChannelKind.OTHER_HUB
    assert c.classify(ChannelRef(id="u", name="unrelated", guild_id="1")).kind_hint is ChannelKind.UNKNOWN


def test_forum_provisioner_creates_threads_and_webhooks_and_is_idempotent():
    d = FakeDiscord()
    prov = ForumProvisioner(d, category_id=HUB_CATEGORY)
    customer = Customer(CUSTOMER, "Alice Smith", 1, False, 0, 10**10, True)
    out = run(prov.ensure(customer))
    assert out.created and set(out.thread_ids) == {r for r, _, _ in THREADS}
    assert out.webhooks.complete() and out.webhooks.dm == ""       # non-VIP → no dm-inbox
    assert "thread_id=" + out.thread_ids["dashboard"] in out.webhooks.dashboard
    assert d.channels[out.forum_id].name == "alice-smith-hub"
    # upgrade to VIP → only the dm-inbox thread is added, forum reused
    vip = customer.with_(vip=True, forum_id=out.forum_id, thread_ids=out.thread_ids)
    out2 = run(prov.ensure(vip))
    assert not out2.created and out2.forum_id == out.forum_id
    assert set(out2.thread_ids) == {r for r, _, _ in THREADS + VIP_THREADS} and out2.webhooks.dm
    assert len(d.forums) == 1
    assert run(prov.lock(vip)) and d.readonly[out.forum_id] is True
    assert run(prov.unlock(vip)) and d.readonly[out.forum_id] is False
    assert run(prov.delete(vip)) and out.forum_id in d.deleted


def test_embeds_are_bounded_and_render_to_dict():
    now = 1000.0
    c = Customer(CUSTOMER, "alice", 2, True, 0, now + 3 * DAY, True)
    a = Alt(CUSTOMER, 1, 1, "w", "alice_alt1", username="main", channel_ids=("1", "2"))
    e = account_embed(c, [a], now, policy_acked=False)
    d = e.to_dict()
    assert d["title"].startswith("Account") and any("VIP" in f["value"] for f in d["fields"])
    st = alt_status_embed(a, None, None, now)
    assert "offline" in st.to_dict()["fields"][0]["value"]
    h = help_embed(Tier.CUSTOMER)
    names = [f["name"] for f in h.to_dict()["fields"]]
    assert "/run" in names and "/admin" not in names and "/vip" not in names
    assert "/vip" in [f["name"] for f in help_embed(Tier.VIP).to_dict()["fields"]]
    long = Reply.ok("x" * 10)
    assert long.as_dict()["ephemeral"] is True and Reply.public("hi").ephemeral is False
