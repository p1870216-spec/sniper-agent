"""Управление правилами tweet_filter на twitterapi.io.

Правила живут на серверах twitterapi.io, а не у тебя: они продолжают
работать и тарифицироваться, даже когда компьютер выключен. Поэтому
перед долгой паузой правило надо гасить руками.

    python tools/rules.py list    что сейчас заведено
    python tools/rules.py sync    пересоздать под config/kols.yml и включить
    python tools/rules.py off     выключить (обратимо, денег не тратит)
    python tools/rules.py on      включить обратно

sync сносит свои прежние правила и создаёт заново — иначе повторный
вызов удвоил бы счёт за проверки. Чужие правила не трогает.

Webhook URL через API не задаётся: он один на аккаунт и вписывается
в веб-кабинете twitterapi.io.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings, load_kols            # noqa: E402
from app.providers.twitterapi_io import (                 # noqa: E402
    RULE_TAG_PREFIX,
    TwitterApiIoClient,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("rules")

FIELDS = ("rule_id", "tag", "value", "interval_seconds", "is_effect",
          "last_tweet_id", "cost_credit")


def show(rule: dict) -> None:
    state = "ВКЛ " if rule.get("is_effect") else "выкл"
    print(f"  [{state}] {rule.get('tag')}  {rule.get('rule_id')}")
    print(f"         value: {rule.get('value')}")
    print(
        f"         интервал {rule.get('interval_seconds')}с · "
        f"последний твит {rule.get('last_tweet_id')} · "
        f"потрачено {rule.get('cost_credit')}"
    )


async def main(command: str) -> int:
    client = TwitterApiIoClient(get_settings())
    try:
        rules = await client.list_rules()

        if command == "list":
            if not rules:
                print("правил нет")
            for rule in rules:
                show(rule)
            print()
            print("last_tweet_id = 0 при включённом правиле значит, что оно")
            print("ещё ничего не обработало — смотри подписку в кабинете.")
            return 0

        if command == "sync":
            handles = list(load_kols().keys())
            print(f"аккаунты из kols.yml ({len(handles)}): {', '.join(handles)}")
            ids = await client.register_stream(handles)
            if not ids:
                print("НЕ создано ни одного правила")
                return 1
            for rule in await client.list_rules():
                show(rule)
            return 0

        if command in ("on", "off"):
            mine = [r for r in rules
                    if str(r.get("tag", "")).startswith(RULE_TAG_PREFIX)]
            if not mine:
                print("своих правил не найдено — сначала sync")
                return 1
            for rule in mine:
                await client.set_rule_effect(rule, command == "on")
                print(f"{rule['tag']}: {'включено' if command == 'on' else 'выключено'}")
            return 0

        print(f"неизвестная команда: {command}")
        print(__doc__)
        return 2
    finally:
        await client.aclose()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    sys.exit(asyncio.run(main(cmd)))
