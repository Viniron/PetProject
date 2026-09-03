"""Клиент Backblaze B2 - ровно столько, сколько нужно для выгрузки дампа.

Почему нативный API B2, а не S3-совместимый: у B2 есть S3-шлюз, но работа
с ним требует awscli или boto3, то есть десятков мегабайт зависимостей в
образе бэкапа. Нативный API - три запроса на стандартной библиотеке.

Почему не сторонний SDK: CLAUDE.md запрещает добавлять зависимости без
спроса, а выгрузка одного файла того не стоит.

Ключ доступа выдаётся только на запись (ADR-020), поэтому здесь нет ни
удаления, ни скачивания: их нечем было бы выполнить, и это защита от
сценария "ошибка в скрипте на Pi стёрла историю копий".
"""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Предел одиночной загрузки в B2. Больше - только через large-file API,
# которого здесь нет намеренно: дамп такого размера означает, что схему
# бэкапа надо пересматривать, а не молча дробить файл.
MAX_SINGLE_UPLOAD_BYTES = 5 * 1024**3


class B2Error(RuntimeError):
    """Отказ B2. Отдельный тип, чтобы джоб не ловил всё подряд."""


class B2Client:
    """Тонкий клиент: авторизация, получение адреса загрузки, загрузка файла.

    Состояние после authorize() хранится в полях: B2 требует передавать
    полученный токен и адрес API в каждый следующий запрос.
    """

    def __init__(self, api_url: str, key_id: str, app_key: str, timeout: int) -> None:
        self._entry_url = api_url.rstrip("/")
        self._key_id = key_id
        self._app_key = app_key
        self._timeout = timeout
        self._api_url: str = ""
        self._token: str = ""
        self._account_id: str = ""
        self._scoped_bucket_id: str = ""

    def _request(
        self,
        url: str,
        *,
        headers: dict[str, str],
        data: bytes | None = None,
        method: str = "GET",
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body: bytes = response.read()
        except urllib.error.HTTPError as error:
            # Тело ошибки B2 - JSON с кодом и человеческим текстом; без него
            # диагностика сводится к "500", и это худший вид отказа.
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise B2Error(f"B2 ответил {error.code} на {method} {url}: {detail}") from error
        except urllib.error.URLError as error:
            raise B2Error(f"B2 недоступен ({method} {url}): {error.reason}") from error

        parsed: Any = json.loads(body)
        if not isinstance(parsed, dict):
            raise B2Error(f"B2 вернул не объект на {method} {url}")
        return parsed

    def authorize(self) -> None:
        """Авторизация. Возвращает адрес API и токен, живущий сутки."""
        credentials = base64.b64encode(f"{self._key_id}:{self._app_key}".encode()).decode()
        answer = self._request(
            f"{self._entry_url}/b2api/v3/b2_authorize_account",
            headers={"Authorization": f"Basic {credentials}"},
        )

        storage = answer.get("apiInfo", {}).get("storageApi", {})
        self._api_url = str(storage.get("apiUrl", "")).rstrip("/")
        self._token = str(answer.get("authorizationToken", ""))
        self._account_id = str(answer.get("accountId", ""))
        # Ключ, выданный на один бакет, приносит его id сразу - тогда
        # b2_list_buckets не нужен, и права на список бакетов тоже.
        self._scoped_bucket_id = str(storage.get("bucketId") or "")

        if not self._api_url or not self._token:
            raise B2Error("B2 не вернул apiUrl или токен - проверить ключ")

    def bucket_id(self, bucket_name: str) -> str:
        """Идентификатор бакета по имени."""
        if self._scoped_bucket_id:
            return self._scoped_bucket_id

        answer = self._request(
            f"{self._api_url}/b2api/v3/b2_list_buckets?"
            + urllib.parse.urlencode({"accountId": self._account_id, "bucketName": bucket_name}),
            headers={"Authorization": self._token},
        )
        buckets: Any = answer.get("buckets", [])
        if not isinstance(buckets, list) or not buckets:
            raise B2Error(f"бакет {bucket_name!r} не найден или ключ не даёт его увидеть")
        return str(buckets[0]["bucketId"])

    def upload(self, bucket_name: str, remote_name: str, source: Path, sha1: str) -> str:
        """Загружает файл под именем remote_name. Возвращает fileId.

        sha1 передаётся заголовком: B2 сверяет его сам и отвергает загрузку,
        битую в пути. Это единственная проверка целостности, которая работает
        до того, как копия понадобится.
        """
        size = source.stat().st_size
        if size > MAX_SINGLE_UPLOAD_BYTES:
            raise B2Error(
                f"дамп {size} байт больше предела одиночной загрузки "
                f"{MAX_SINGLE_UPLOAD_BYTES}: нужен large-file API"
            )

        target = self._request(
            f"{self._api_url}/b2api/v3/b2_get_upload_url?"
            + urllib.parse.urlencode({"bucketId": self.bucket_id(bucket_name)}),
            headers={"Authorization": self._token},
        )
        upload_url = str(target["uploadUrl"])
        upload_token = str(target["authorizationToken"])

        with source.open("rb") as stream:
            request = urllib.request.Request(
                upload_url,
                data=stream,
                method="POST",
                headers={
                    "Authorization": upload_token,
                    # Имя файла идёт в заголовке, поэтому обязано быть
                    # процентно-закодированным - иначе любой небезопасный
                    # символ в имени сломает запрос.
                    "X-Bz-File-Name": urllib.parse.quote(remote_name),
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                    "X-Bz-Content-Sha1": sha1,
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    answer: Any = json.loads(response.read())
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:500]
                raise B2Error(f"B2 отверг загрузку ({error.code}): {detail}") from error
            except urllib.error.URLError as error:
                raise B2Error(f"загрузка в B2 не дошла: {error.reason}") from error

        if not isinstance(answer, dict):
            raise B2Error("B2 вернул не объект на загрузку")
        # Сверяем возвращённую сумму со своей: совпадение означает, что в
        # бакете лежит именно тот файл, который мы читали с диска.
        if str(answer.get("contentSha1", "")) != sha1:
            raise B2Error(
                f"B2 подтвердил другую сумму: ожидалась {sha1}, "
                f"получена {answer.get('contentSha1')!r}"
            )
        return str(answer["fileId"])
