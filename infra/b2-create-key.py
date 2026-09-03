#!/usr/bin/env python3
"""Создаёт ключ B2 с правами, которых требует ADR-020, и вписывает его в .env.

Зачем отдельный инструмент. Веб-интерфейс Backblaze предлагает три готовых
набора прав, и ни один не годится: «Write Only» включает `deleteFiles`
и не включает `listFiles`. То есть ключ на плате может стереть всю историю
копий, но не может её перечислить - ровно наоборот к тому, что нужно.
Поштучно права задаются только через API, что этот скрипт и делает.

Мастер-ключ спрашивается при запуске, нигде не сохраняется и живёт только
в памяти процесса. Новый ключ вписывается в .env сам - чтобы длинная строка
не проходила через буфер обмена и глаза.

Все приглашения ко вводу - латиницей намеренно. readline декодирует
промпт по локали системы, и русский текст в input() валит его с
UnicodeDecodeError, хотя print тем же текстом работает. Проверено
на плате: скрипт упал ровно на вопросе об удалении старого ключа.

Запуск на хосте, где лежит .env:
    python3 infra/b2-create-key.py
"""

import base64
import getpass
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API_ENTRY = "https://api.backblazeb2.com"
KEY_NAME = "jarvis-backup"

# Ровно два права. writeFiles - выгрузка дампа; listFiles - возможность
# проверить, что копия действительно появилась в бакете (проверка «спросить
# у облака», а не «поверить логу джоба»). Удаления нет намеренно: удалять
# умеет только облако, правилами жизненного цикла.
CAPABILITIES = ["writeFiles", "listFiles"]

TIMEOUT = 30

# Подсказка при 401. Без неё сообщение «B2 ответил 401» не говорит ничего,
# а причин ровно четыре, и все проверяются глазами за минуту.
ПОДСКАЗКА_401 = [
    "B2 ответил 401 - ключ не признан. Проверить по порядку:",
    "  1. это МАСТЕР-ключ (кнопка Generate New Master Application Key),",
    "     а не обычный ключ из списка ниже на той же странице;",
    "  2. значения не перепутаны местами: keyID короче, applicationKey",
    "     длиннее и обычно начинается с K;",
    "  3. значение скопировано целиком - при вставке правым кликом хвост",
    "     иногда обрезается, а перевод строки внутри обрывает ввод;",
    "  4. мастер-ключ не перевыпускался после копирования: новая генерация",
    "     делает прежний недействительным сразу.",
]


def запрос(url: str, token: str, данные: dict[str, object] | None = None) -> dict[str, object]:
    тело = json.dumps(данные).encode() if данные is not None else None
    заголовки = {"Authorization": token}
    if тело is not None:
        заголовки["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=тело, headers=заголовки)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as ответ:
            результат: dict[str, object] = json.load(ответ)
            return результат
    except urllib.error.HTTPError as ошибка:
        подробности = ошибка.read().decode("utf-8", errors="replace")[:400]
        if ошибка.code == 401:
            for строка in ПОДСКАЗКА_401:
                print(строка, file=sys.stderr)
            sys.exit(f"Ответ B2: {подробности}")
        sys.exit(f"B2 ответил {ошибка.code}: {подробности}")
    except urllib.error.URLError as ошибка:
        sys.exit(f"B2 недоступен: {ошибка.reason}")


def читать_env(путь: pathlib.Path) -> dict[str, str]:
    значения: dict[str, str] = {}
    for строка in путь.read_text(encoding="utf-8").splitlines():
        if "=" in строка and not строка.lstrip().startswith("#"):
            ключ, значение = строка.split("=", 1)
            значения[ключ.strip()] = значение.strip()
    return значения


def записать_env(путь: pathlib.Path, замены: dict[str, str]) -> None:
    """Заменяет значения по месту, сохраняя порядок строк и комментарии."""
    строки = путь.read_text(encoding="utf-8").splitlines(keepends=True)
    осталось = dict(замены)
    for номер, строка in enumerate(строки):
        совпадение = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=)", строка)
        if совпадение and совпадение.group(2) in осталось:
            имя = совпадение.group(2)
            строки[номер] = f"{имя}={осталось.pop(имя)}\n"
    # Переменных, которых в файле не было, дописываем в конец.
    for имя, значение in осталось.items():
        строки.append(f"{имя}={значение}\n")
    путь.write_text("".join(строки), encoding="utf-8")


def main() -> int:
    env_путь = pathlib.Path(__file__).resolve().parent.parent / ".env"
    if not env_путь.exists():
        sys.exit(f"нет файла {env_путь}")

    # Скрипт спрашивает мастер-ключ с клавиатуры, поэтому в неинтерактивной
    # оболочке он не работал бы, а молча ждал ввода. Проверка добавлена после
    # того, как именно это и случилось при попытке запустить его из скрипта.
    if not sys.stdin.isatty():
        sys.exit("нужен интерактивный терминал: скрипт спрашивает мастер-ключ с клавиатуры")

    env = читать_env(env_путь)
    бакет = env.get("B2_BUCKET", "")
    if not бакет:
        sys.exit("в .env не задан B2_BUCKET")

    print(f"бакет: {бакет}")
    print("Нужен МАСТЕР-ключ аккаунта (App Keys -> Generate New Master Application Key).")
    print("Он не сохраняется и нужен только на время этого запуска.")
    print()
    master_id = input("master keyID: ").strip()
    print("Ввод applicationKey не отображается - это нормально.")
    master_key = getpass.getpass("master applicationKey: ").strip()
    if not master_id or not master_key:
        sys.exit("пустой ввод - выхожу")

    # Значения не печатаем, только длины: по ним видно обрезанную вставку,
    # а сам ключ в историю терминала не попадает.
    print()
    print(f"принято: keyID {len(master_id)} симв., applicationKey {len(master_key)} симв.")
    print("для справки: у МАСТЕР-ключа keyID это id аккаунта, около 12 симв.,")
    print("а сам applicationKey около 42; у обычных ключей - около 25 и 31")
    if len(master_id) < 10 or len(master_key) < 20:
        print("ВНИМАНИЕ: похоже на обрезанную вставку - проверьте значения")
    if master_id.startswith("K00") and not master_key.startswith("K00"):
        print("ВНИМАНИЕ: значения похожи на перепутанные местами")
    print()

    креды = base64.b64encode(f"{master_id}:{master_key}".encode()).decode()
    авторизация = запрос(f"{API_ENTRY}/b2api/v3/b2_authorize_account", f"Basic {креды}")
    хранилище = авторизация["apiInfo"]["storageApi"]  # type: ignore[index]
    api = str(хранилище["apiUrl"]).rstrip("/")  # type: ignore[index]
    token = str(авторизация["authorizationToken"])
    account = str(авторизация["accountId"])

    список = запрос(
        f"{api}/b2api/v3/b2_list_buckets?"
        + urllib.parse.urlencode({"accountId": account, "bucketName": бакет}),
        token,
    )
    бакеты = список.get("buckets", [])
    if not isinstance(бакеты, list) or not бакеты:
        sys.exit(f"бакет {бакет!r} не найден в аккаунте")
    bucket_id = str(бакеты[0]["bucketId"])

    новый = запрос(
        f"{api}/b2api/v3/b2_create_key",
        token,
        {
            "accountId": account,
            "keyName": KEY_NAME,
            "capabilities": CAPABILITIES,
            "bucketId": bucket_id,
        },
    )
    новый_id = str(новый["applicationKeyId"])
    новый_ключ = str(новый["applicationKey"])
    старый_id = env.get("B2_KEY_ID", "")

    записать_env(env_путь, {"B2_KEY_ID": новый_id, "B2_APP_KEY": новый_ключ})
    print()
    print(f"новый ключ создан и вписан в {env_путь}")
    print(f"права: {', '.join(str(c) for c in новый.get('capabilities', []))}")
    print("deleteFiles: нет" if "deleteFiles" not in CAPABILITIES else "deleteFiles: ЕСТЬ")

    if старый_id and старый_id != новый_id:
        print()
        print(f"Старый ключ {старый_id[:8]}... остаётся действующим.")
        print("Он имеет право удалять файлы, поэтому его лучше отозвать.")
        print("Удалить старый ключ?")
        ответ = input("[y/N]: ").strip().lower()
        if ответ == "y":
            запрос(f"{api}/b2api/v3/b2_delete_key", token, {"applicationKeyId": старый_id})
            print("старый ключ удалён")
        else:
            print("оставлен - удалите его вручную в App Keys")

    print()
    print("Готово. Проверить: make backup (dry-run), затем make backup-apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
