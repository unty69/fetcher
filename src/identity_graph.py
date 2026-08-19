"""
identity_graph.py — identity graph по co-occurrence контактов (телефон/
username/email) в одном сообщении, поверх всех cache/dump*.jsonl.

Только ПРЯМОЕ co-occurrence: ребро — для контактов, встретившихся ВМЕСТЕ в
одном сообщении. Никаких транзитивных "слабых" связей (общий канал, похожий
шаблон текста и т.п.) в рёбра не добавляется. Компоненты связности графа
(сами по себе транзитивны через цепочки прямых рёбер — это и есть их смысл)
= canonical identity.

Не переписывает crawl.py/resolve.py/identify.py/build.py — только
переиспользует normalize_contact() и union-find (_find/_union), найденные в
build.py (см. ЭТАП 0).
"""

import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build import _find, _union, normalize_contact  # noqa: E402

CACHE_DIR = BASE_DIR / "cache"
OUTPUT_PATH = CACHE_DIR / "identity_graph.json"

# ">50 контактов в одном компоненте" из ТЗ — сигнал переслипания (общий
# шаблон/агентство), не жёсткое обоснование, взято как есть.
SUSPND_THRESHOLD = 50

# "usable" — телефон/username (прямой канал связи). email намеренно не
# usable сам по себе — см. ЭТАП 0 (решение по "ссылка-identity").
USABLE_TYPES = {"phone", "username"}


# --- загрузка дампов --------------------------------------------------------


def iter_dump_paths():
    return sorted(CACHE_DIR.glob("dump*.jsonl"))


def load_messages(dump_paths=None):
    """Читает и объединяет JSONL-дампы, дедуплицируя по (channel,
    message_id) — dump_depth_probe.jsonl частично пересекается с
    dump.jsonl/dump_tier2.jsonl (более глубокий повторный обход тех же
    каналов, см. ЭТАП 0), первое встреченное вхождение побеждает (порядок
    файлов — dump.jsonl раньше depth_probe при сортировке по имени)."""
    if dump_paths is None:
        dump_paths = iter_dump_paths()
    seen_ids = set()
    messages = []
    for path in dump_paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
                key = (msg["channel"], msg["message_id"])
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                messages.append(msg)
    return messages


# --- построение графа --------------------------------------------------------


def message_contact_nodes(msg):
    """[(node_key, contact_type, normalized_value, raw_value)] уникальных
    контактов сообщения. node_key = "type:normalized" — с типом в ключе,
    чтобы разные типы контактов не могли случайно совпасть по значению.
    Дедуп по node_key (не по исходному raw_value, как в crawl.py) — два
    разных написания одного телефона в одном сообщении не должны давать
    самопересекающееся ребро/задвоенный узел."""
    seen = set()
    nodes = []
    for c in msg["contacts"]:
        normalized = normalize_contact(c["type"], c["value"])
        if not normalized:
            continue
        node_key = f'{c["type"]}:{normalized}'
        if node_key in seen:
            continue
        seen.add(node_key)
        nodes.append((node_key, c["type"], normalized, c["value"]))
    return nodes


def build_graph(messages):
    """Строит граф co-occurrence по переданным сообщениям. Возвращает
    (node_info, parent): node_info — node_key -> метаданные узла (тип,
    значение, каналы, сообщения, raw-варианты, даты, direct_neighbor_types —
    множество типов контактов, с которыми узел НАПРЯМУЮ встречался в одном
    сообщении; нужно для measured enrichment yield в novelty.py); parent —
    union-find для компонент."""
    node_info = {}
    parent = {}

    def ensure_node(node_key, contact_type, normalized):
        if node_key not in node_info:
            node_info[node_key] = {
                "type": contact_type,
                "value": normalized,
                "raw_values": set(),
                "channels": set(),
                "messages": set(),
                "direct_neighbor_types": set(),
                "first_seen": None,
                "last_seen": None,
            }
            parent[node_key] = node_key
        return node_info[node_key]

    for msg in messages:
        nodes = message_contact_nodes(msg)
        if not nodes:
            continue
        dt = datetime.fromisoformat(msg["date"])

        for node_key, ctype, normalized, raw in nodes:
            info = ensure_node(node_key, ctype, normalized)
            info["raw_values"].add(raw)
            info["channels"].add(msg["channel"])
            info["messages"].add((msg["channel"], msg["message_id"]))
            if info["first_seen"] is None or dt < info["first_seen"]:
                info["first_seen"] = dt
            if info["last_seen"] is None or dt > info["last_seen"]:
                info["last_seen"] = dt

        # Co-occurrence: ребро на каждую пару контактов ЭТОГО сообщения
        # (клика внутри сообщения) — прямое совместное появление, без
        # обращения к другим сообщениям.
        for (ka, ta, na, ra), (kb, tb, nb, rb) in combinations(nodes, 2):
            node_info[ka]["direct_neighbor_types"].add(tb)
            node_info[kb]["direct_neighbor_types"].add(ta)
            _union(parent, ka, kb)

    return node_info, parent


def compute_components(node_info, parent):
    """root -> [node_key, ...] для КАЖДОГО узла в node_info, включая
    изолированные (сообщение с ровно одним контактом даёт узел без рёбер —
    отдельный canonical identity размера 1)."""
    groups = defaultdict(list)
    for node_key in node_info:
        root = _find(parent, node_key)
        groups[root].append(node_key)
    return groups


def canonical_id_for(member_keys):
    """Стабильный id компонента — хэш отсортированных ключей участников, а
    не порядковый номер: не съезжает между прогонами при появлении новых
    компонент (файл предназначен для использования следующими шагами)."""
    digest = hashlib.sha1("|".join(sorted(member_keys)).encode("utf-8")).hexdigest()
    return f"cid_{digest[:10]}"


def build_identities(node_info, groups):
    """root -> identity dict: контакты компонента + агрегаты (suspND,
    usable, alt-cluster, enrichment yield). Отдельно от compute_components(),
    чтобы граф (node_info/parent/groups) и бизнes-правила (порог suspND,
    usable) не были перемешаны в одном проходе."""
    identities = {}
    for root, member_keys in groups.items():
        cid = canonical_id_for(member_keys)
        members = [node_info[k] for k in member_keys]
        phone_count = sum(1 for m in members if m["type"] == "phone")
        username_count = sum(1 for m in members if m["type"] == "username")
        size = len(members)

        channels = set()
        all_messages = set()
        for m in members:
            channels |= m["channels"]
            all_messages |= m["messages"]  # union, не сумма — иначе сообщение с 2 контактами компонента считалось бы дважды

        first_seen = min((m["first_seen"] for m in members if m["first_seen"]), default=None)
        last_seen = max((m["last_seen"] for m in members if m["last_seen"]), default=None)

        usable = phone_count > 0 or username_count > 0
        # Email-узел, у которого НИ В ОДНОМ сообщении не было прямого
        # соседа-телефона/username — его usability (если она вообще есть)
        # доказана только транзитивностью графа (через другой узел из
        # ДРУГОГО сообщения), не наблюдаема из одного сообщения напрямую.
        email_bridge_nodes = [
            m for m in members
            if m["type"] == "email" and not (m["direct_neighbor_types"] & USABLE_TYPES)
        ]

        identities[cid] = {
            "canonical_id": cid,
            "member_keys": member_keys,
            "size": size,
            "phone_count": phone_count,
            "username_count": username_count,
            "email_count": size - phone_count - username_count,
            "usable": usable,
            "is_alt_cluster": phone_count >= 2 or username_count >= 2,
            "suspected_non_discovery": size > SUSPND_THRESHOLD,
            "graph_enriched_email_count": len(email_bridge_nodes),
            "gained_usability_via_graph": bool(usable and email_bridge_nodes),
            "channels": channels,
            "message_count": len(all_messages),
            "first_seen": first_seen,
            "last_seen": last_seen,
        }
    return identities


def build_identity_graph(messages):
    """Собрать граф + компоненты + identities по переданным сообщениям.
    Публичная точка входа для переиспользования из novelty.py. Возвращает
    (node_info, identities)."""
    node_info, parent = build_graph(messages)
    groups = compute_components(node_info, parent)
    identities = build_identities(node_info, groups)
    return node_info, identities


# --- сериализация в cache/identity_graph.json --------------------------------


def serialize_identities(identities, node_info):
    contact_to_canonical = {}
    identities_out = {}
    for cid, ident in identities.items():
        contacts = []
        for k in ident["member_keys"]:
            info = node_info[k]
            contacts.append({
                "type": info["type"],
                "value": info["value"],
                "raw_values": sorted(info["raw_values"])[:5],
            })
            contact_to_canonical[k] = cid
        identities_out[cid] = {
            "canonical_id": cid,
            "contacts": contacts,
            "size": ident["size"],
            "phone_count": ident["phone_count"],
            "username_count": ident["username_count"],
            "email_count": ident["email_count"],
            "usable": ident["usable"],
            "is_alt_cluster": ident["is_alt_cluster"],
            "suspected_non_discovery": ident["suspected_non_discovery"],
            "gained_usability_via_graph": ident["gained_usability_via_graph"],
            "channels": sorted(ident["channels"]),
            "message_count": ident["message_count"],
            "first_seen": ident["first_seen"].isoformat() if ident["first_seen"] else None,
            "last_seen": ident["last_seen"].isoformat() if ident["last_seen"] else None,
        }
    return identities_out, contact_to_canonical


def save_identity_graph(identities_out, contact_to_canonical, dump_paths, path=OUTPUT_PATH):
    payload = {
        "generated_at": datetime.now().isoformat(),
        "source_dumps": [str(p.relative_to(BASE_DIR)) for p in dump_paths],
        "params": {"suspnd_threshold": SUSPND_THRESHOLD, "usable_types": sorted(USABLE_TYPES)},
        "identity_count": len(identities_out),
        "identities": identities_out,
        "contact_to_canonical": contact_to_canonical,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# --- вывод статистики (ЭТАП 3 ТЗ) -------------------------------------------


def format_contacts(member_keys, node_info, limit=30):
    shown = member_keys[:limit]
    parts = [f'{node_info[k]["type"]}:{node_info[k]["value"]}' for k in shown]
    text = ", ".join(parts)
    if len(member_keys) > limit:
        text += f", ... ещё {len(member_keys) - limit}"
    return text


def print_stats(messages, node_info, identities):
    non_suspnd = [i for i in identities.values() if not i["suspected_non_discovery"]]
    suspnd = [i for i in identities.values() if i["suspected_non_discovery"]]
    alt_clusters_all = sorted(
        (i for i in identities.values() if i["is_alt_cluster"]),
        key=lambda i: i["size"], reverse=True,
    )
    alt_clusters_suspnd = [i for i in alt_clusters_all if i["suspected_non_discovery"]]
    enriched = [i for i in non_suspnd if i["gained_usability_via_graph"]]

    type_counts = defaultdict(int)
    for info in node_info.values():
        type_counts[info["type"]] += 1

    print(f"Сообщений в объединённом корпусе (после дедупа по channel+message_id): {len(messages)}")
    print(f"Сырых уникальных контактов всего: {len(node_info)} "
          f"(phone: {type_counts['phone']}, username: {type_counts['username']}, email: {type_counts['email']})")
    print(f"Canonical identity после склейки (без suspND): {len(non_suspnd)}")
    print(f"  из них usable (телефон/username): {sum(1 for i in non_suspnd if i['usable'])}")
    print(f"Мега-компонентов помечено suspND (>{SUSPND_THRESHOLD} контактов): {len(suspnd)} "
          f"(суммарно {sum(i['size'] for i in suspnd)} контактов внутри них — "
          f"исключены из числа canonical identity выше, не считаются одной личностью)")
    print(f"Alt-кластеров (2+ телефона ИЛИ 2+ username) всего: {len(alt_clusters_all)} "
          f"(из них suspND: {len(alt_clusters_suspnd)}, не suspND: {len(alt_clusters_all) - len(alt_clusters_suspnd)})")
    print("Measured enrichment yield: identity, у которых НИ В ОДНОМ сообщении email "
          "не соседствовал с телефоном/username напрямую, но usable-контакт получен через "
          "co-occurrence в графе (транзитивно, не suspND): "
          f"{len(enriched)}")

    print("\nТоп-10 alt-кластеров по размеру (canonical_id -> контакты):")
    for i, ident in enumerate(alt_clusters_all[:10], 1):
        flag = " [suspND]" if ident["suspected_non_discovery"] else ""
        print(f"{i}. {ident['canonical_id']}{flag} — size={ident['size']} "
              f"(phone={ident['phone_count']}, username={ident['username_count']}, email={ident['email_count']}), "
              f"channels={len(ident['channels'])}, messages={ident['message_count']}")
        print(f"   {format_contacts(ident['member_keys'], node_info)}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    dump_paths = iter_dump_paths()
    print("Дампы:")
    for p in dump_paths:
        print(f"  {p.relative_to(BASE_DIR)}")
    print()

    messages = load_messages(dump_paths)
    node_info, identities = build_identity_graph(messages)
    identities_out, contact_to_canonical = serialize_identities(identities, node_info)
    save_identity_graph(identities_out, contact_to_canonical, dump_paths)

    print_stats(messages, node_info, identities)
    print(f"\nСохранено: {OUTPUT_PATH.relative_to(BASE_DIR)}")


if __name__ == "__main__":
    main()
