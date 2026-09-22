"""祖先来源反向追踪 GET /sources/{sample_id}/containers 的接口测试。"""
from decimal import Decimal

from helpers import container, sample


def _batch(client, transfers, key=None):
    body = {"transfers": transfers}
    if key is not None:
        body["idempotency_key"] = key
    r = client.post("/transfers/batch", json=body)
    assert r.status_code == 200, r.text
    return r


def _map(client, sid, **params):
    r = client.get(f"/sources/{sid}/containers", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    return {e["container_id"]: e for e in body["containers"]}, body


def test_revision_starts_zero_and_empty_result_for_untraced_sample(client):
    sample(client, "S1", "100")
    r = client.get("/sources/S1/containers")
    assert r.status_code == 200
    body = r.json()
    # 尚未发生任何成功批次：修订号为 0；初始样品自身持有全部份额，仍被列出
    assert body["transfer_revision"] == 0
    assert body["count"] == 1
    assert body["next_cursor"] is None
    assert body["containers"] == [
        {
            "container_id": "S1",
            "volume": "100.000000000",
            "source_amount": "100.000000000",
            "proportion": "1.000000",
        }
    ]


def test_trace_follows_lineage_and_reports_volume_amount_proportion(client):
    sample(client, "S1", "100")
    sample(client, "S2", "100")
    container(client, "V1")
    container(client, "V2")
    _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "40"},
            {"source": "S2", "destination": "V1", "volume": "60"},
            {"source": "V1", "destination": "V2", "volume": "25"},
        ],
    )

    entries, body = _map(client, "S1")
    # 成功批次提交后全局修订号恰为 1
    assert body["transfer_revision"] == 1
    # 结果按 container_id 字典序：S1、V1、V2
    assert list(entries) == ["S1", "V1", "V2"]
    assert entries["S1"]["volume"] == "60.000000000"
    assert entries["S1"]["source_amount"] == "60.000000000"
    assert entries["S1"]["proportion"] == "1.000000"
    # V1 总体积 75（S1 30 + S2 45），S1 份额 30 占比 0.4
    assert entries["V1"]["volume"] == "75.000000000"
    assert entries["V1"]["source_amount"] == "30.000000000"
    assert entries["V1"]["proportion"] == "0.400000"
    # V2 总体积 25（S1 10 + S2 15），S1 份额 10 占比 0.4
    assert entries["V2"]["volume"] == "25.000000000"
    assert entries["V2"]["source_amount"] == "10.000000000"
    assert entries["V2"]["proportion"] == "0.400000"

    # 追踪 S2：S1 容器不应出现
    entries_s2, _ = _map(client, "S2")
    assert set(entries_s2) == {"S2", "V1", "V2"}
    # 跨容器绝对份额守恒：S2 初始 100 = 40(S2自留) + 45(V1) + 15(V2)
    traced = sum(
        (Decimal(e["source_amount"]) for e in entries_s2.values()), Decimal(0)
    )
    assert traced == Decimal("100.000000000")


def test_exhausted_sample_container_drops_out_of_trace(client):
    sample(client, "S1", "10")
    container(client, "V1")
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "10"}])
    entries, _ = _map(client, "S1")
    # S1 自身份额已为 0，不再列出；只有 V1
    assert list(entries) == ["V1"]
    assert entries["V1"]["source_amount"] == "10.000000000"
    assert entries["V1"]["proportion"] == "1.000000"


def test_unknown_and_non_sample_ids_404(client):
    sample(client, "S1", "10")
    container(client, "V1")
    for sid in ("GHOST", "V1"):
        r = client.get(f"/sources/{sid}/containers")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "unknown_sample"


def test_min_amount_filter(client):
    sample(client, "S1", "100")
    container(client, "V1")
    container(client, "V2")
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "5"}])
    _batch(client, [{"source": "S1", "destination": "V2", "volume": "30"}])

    # 阈值 10：保留 S1(65) 与 V2(30)，过滤掉 V1(5)
    entries, body = _map(client, "S1", min_amount="10")
    assert set(entries) == {"S1", "V2"}
    assert body["min_amount"] == "10.000000000"

    # 闭区间：恰好等于阈值的容器保留
    entries, _ = _map(client, "S1", min_amount="30")
    assert set(entries) == {"S1", "V2"}

    # 阈值高于所有份额：空结果且无游标
    entries, body = _map(client, "S1", min_amount="999")
    assert entries == {}
    assert body["next_cursor"] is None
    assert body["count"] == 0


def test_min_amount_invalid_422(client):
    sample(client, "S1", "10")
    for bad in ("-1", "abc", "0.0000000001"):
        r = client.get("/sources/S1/containers", params={"min_amount": bad})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "invalid_volume"


def test_limit_validation_422(client):
    sample(client, "S1", "10")
    for bad in (0, -5, 101, 1000):
        r = client.get("/sources/S1/containers", params={"limit": bad})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "invalid_limit"


def test_cursor_pagination_is_stable_and_complete(client):
    sample(client, "S1", "100")
    for cid in ("V1", "V2", "V3", "V4", "V5"):
        container(client, cid)
    _batch(
        client,
        [
            {"source": "S1", "destination": "V1", "volume": "10"},
            {"source": "S1", "destination": "V2", "volume": "10"},
            {"source": "S1", "destination": "V3", "volume": "10"},
            {"source": "S1", "destination": "V4", "volume": "10"},
            {"source": "S1", "destination": "V5", "volume": "10"},
        ],
    )

    # 不分页的完整结果（含自留的 S1，共 6 个容器）
    all_entries, full_body = _map(client, "S1")
    expected_ids = sorted(all_entries)
    assert expected_ids == ["S1", "V1", "V2", "V3", "V4", "V5"]

    # limit=2 翻页：每页修订号相同、ID 严格有序不重不漏
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        r = client.get("/sources/S1/containers", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["transfer_revision"] == full_body["transfer_revision"]
        ids = [e["container_id"] for e in body["containers"]]
        assert ids == sorted(ids)
        assert len(ids) <= 2
        seen.extend(ids)
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages <= 5
    assert pages == 3
    assert seen == expected_ids


def test_pagination_with_min_amount_keeps_correct_next_page(client):
    sample(client, "S1", "60")
    # V1..V6，份额交替落在阈值 10 上下：V2/V4/V6 各 15，V1/V3/V5 各 5
    specs = {
        "V1": 5, "V2": 15, "V3": 5, "V4": 15, "V5": 5, "V6": 15,
    }
    for cid in specs:
        container(client, cid)
    _batch(
        client,
        [
            {"source": "S1", "destination": cid, "volume": amount}
            for cid, amount in specs.items()
        ],
    )
    # min_amount=10 后 S1 已转空（份额 0）不落页，仅保留 V2/V4/V6
    entries, _ = _map(client, "S1", min_amount="10")
    expected = sorted(entries)
    assert expected == ["V2", "V4", "V6"]

    # limit=1 逐页翻取，被过滤的小份额容器绝不能因“页内满”而错放游标
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 1, "min_amount": "10"}
        if cursor is not None:
            params["cursor"] = cursor
        r = client.get("/sources/S1/containers", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        seen.extend(e["container_id"] for e in body["containers"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == expected


def test_invalid_cursor_400(client):
    sample(client, "S1", "10")
    for bad in ("not-base64!!", "aGVsbG8=", "eyJhIjoxfQ=="):
        r = client.get("/sources/S1/containers", params={"cursor": bad})
        assert r.status_code == 400, (bad, r.text)
        assert r.json()["error"]["code"] == "invalid_cursor"

    # 空字符串游标同样视为失效
    r = client.get("/sources/S1/containers", params={"cursor": ""})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_cursor"


def test_stale_cursor_after_revision_change_409(client):
    sample(client, "S1", "100")
    for cid in ("V1", "V2", "V3"):
        container(client, cid)
    _batch(
        client,
        [{"source": "S1", "destination": cid, "volume": "10"} for cid in ("V1", "V2", "V3")],
    )
    r = client.get("/sources/S1/containers", params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    cursor = body["next_cursor"]
    revision = body["transfer_revision"]
    assert cursor is not None

    # 翻页期间成功提交新批次 -> 修订号 +1 -> 旧游标 409 stale_cursor
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "1"}])
    r = client.get("/sources/S1/containers", params={"limit": 2, "cursor": cursor})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "stale_cursor"
    assert err["details"] == {
        "cursor_revision": revision,
        "current_revision": revision + 1,
    }

    # 以新游标从第一页重新开始可正常翻完
    r = client.get("/sources/S1/containers", params={"limit": 2})
    assert r.status_code == 200
    assert r.json()["transfer_revision"] == revision + 1


def test_revision_only_increments_on_committed_batches(client):
    sample(client, "S1", "100")
    container(client, "V1")
    container(client, "V2")

    def revision():
        return client.get("/sources/S1/containers").json()["transfer_revision"]

    assert revision() == 0
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "10"}])
    assert revision() == 1

    # 失败批次（源液不足）整批回滚：修订号不变
    r = client.post(
        "/transfers/batch",
        json={"transfers": [{"source": "S1", "destination": "V1", "volume": "9999"}]},
    )
    assert r.status_code == 422
    assert revision() == 1

    # 预演不递增
    r = client.post(
        "/transfers/preview",
        json={"transfers": [{"source": "S1", "destination": "V2", "volume": "5"}]},
    )
    assert r.status_code == 200
    assert revision() == 1

    # 幂等重放不递增（同键同请求体返回首次结果，修订号保持）
    _batch(client, [{"source": "S1", "destination": "V2", "volume": "7"}], key="k-1")
    assert revision() == 2
    replay = _batch(
        client, [{"source": "S1", "destination": "V2", "volume": "7"}], key="k-1"
    )
    assert replay.headers.get("idempotency-replayed") == "true"
    assert revision() == 2


def test_amount_fields_are_decimal_strings_not_numbers(client):
    sample(client, "S1", "0.000000001")
    container(client, "V1")
    _batch(client, [{"source": "S1", "destination": "V1", "volume": "0.000000001"}])
    entries, _ = _map(client, "S1")
    assert entries["V1"]["source_amount"] == "0.000000001"
    assert entries["V1"]["volume"] == "0.000000001"
    # 极小份额占比按 PROPORTION_SCALE 四舍五入为 1.000000（V1 只含 S1）
    assert entries["V1"]["proportion"] == "1.000000"
