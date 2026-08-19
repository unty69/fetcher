"""
novelty.py — измерение новизны произвольного набора сообщений ОТНОСИТЕЛЬНО
базового корпуса: семь уровней (new_messages, new_identities, new_contacts,
new_usable_contacts + 3 заготовки) + enrichment_ratio.

new_live_identities / new_confirmed_webmasters / new_relevant_webmasters НЕ
вычисляются здесь: это требует живого сигнала (ответил ли контакт,
подтверждён ли как вебмастер, релевантен ли оффер), которого нет в офлайн-
дампах. Оставлены как None — заполнение принадлежит следующему шагу
(эксперименту), не этому модулю.

Строится поверх identity_graph.py (build_identity_graph, message_contact_nodes)
— не дублирует построение графа/нормализацию контактов.
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from identity_graph import CACHE_DIR, build_identity_graph, load_messages, message_contact_nodes  # noqa: E402


def _message_key(msg):
    return (msg["channel"], msg["message_id"])


def _contact_keys(messages):
    keys = set()
    for m in messages:
        for node_key, *_ in message_contact_nodes(m):
            keys.add(node_key)
    return keys


def compute_novelty(new_messages: list, base_messages: list) -> dict:
    """new_* — то, что появляется в new_messages и отсутствует в
    base_messages.

    canonical identity считается НОВОЙ, только если ВСЕ её контакты
    отсутствуют в base: если хотя бы один контакт компонента уже встречался
    в base, это не новая личность, а новый алиас уже известной (полезный
    факт сам по себе, но не новая identity). suspND-компоненты (см.
    identity_graph.SUSPND_THRESHOLD) исключены из new_identities/
    new_usable_contacts тем же способом, что и в основной статистике
    identity_graph.py — не считаются одной личностью."""
    base_keys = {_message_key(m) for m in base_messages}
    new_only_messages = [m for m in new_messages if _message_key(m) not in base_keys]

    base_contact_keys = _contact_keys(base_messages)
    new_contact_keys = _contact_keys(new_only_messages) - base_contact_keys

    combined = base_messages + new_only_messages
    _, identities = build_identity_graph(combined)

    new_identities = 0
    new_usable = 0
    for ident in identities.values():
        if ident["suspected_non_discovery"]:
            continue
        member_keys = set(ident["member_keys"])
        if member_keys & base_contact_keys:
            continue  # хотя бы один контакт уже был в base -- не новая identity
        if not (member_keys & new_contact_keys):
            continue  # компонент целиком вне base и new (не должно встречаться, защитный случай)
        new_identities += 1
        if ident["usable"]:
            new_usable += 1

    enrichment_ratio = (new_usable / new_identities) if new_identities else None

    return {
        "new_messages": len(new_only_messages),
        "new_identities": new_identities,
        "new_contacts": len(new_contact_keys),
        "new_usable_contacts": new_usable,
        "new_live_identities": None,       # TODO: требует эксперимента -- сигнал живого ответа контакта
        "new_confirmed_webmasters": None,  # TODO: требует эксперимента -- ручное/повторное подтверждение вебмастера
        "new_relevant_webmasters": None,   # TODO: требует эксперимента -- оценка релевантности оффера
        "enrichment_ratio": enrichment_ratio,
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    base_path = CACHE_DIR / "dump.jsonl"
    candidate_paths = [
        CACHE_DIR / "dump_tier2.jsonl",
        CACHE_DIR / "dump_tier3.jsonl",
        CACHE_DIR / "dump_depth_probe.jsonl",
    ]

    base_messages = load_messages([base_path])
    new_messages = load_messages(candidate_paths)

    print("Демонстрация compute_novelty() на реальном корпусе (не входит в обязательный список ТЗ, "
          "показано дополнительно, чтобы подтвердить, что framework реально работает):")
    print(f"  база: {base_path.name} -- {len(base_messages)} сообщений")
    print(f"  новый набор: {', '.join(p.name for p in candidate_paths)} -- {len(new_messages)} сообщений (до вычета пересечения с базой)")
    print()

    result = compute_novelty(new_messages, base_messages)
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
