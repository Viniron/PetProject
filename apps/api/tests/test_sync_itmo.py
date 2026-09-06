"""Джоб забора расписания (Э3): подставной портал, настоящая база.

Проверяется то, ради чего джоб существует, и то, чем он опасен.

Ради: зеркало становится тем, что отдал портал - включая исчезнувшие пары.
Опасен: двойной запуск за день - штатный сценарий catch-up (§11.2), и он
обязан не задваивать; отказ портала не должен стирать зеркало, иначе
пропадает пометка давности, на которой держится инвариант 9.

Сеть замокана целиком. `--dry-run` проверяется буквально: после него в базе
не должно измениться ничего, включая `audit_log`.
"""

import base64
import datetime as dt
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from conftest import загрузить_фикстуру
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jarvis_api.config import Settings
from jarvis_api.db.models import AuditLogEntry, IntegrationToken, ItmoLesson, JobRun, Setting
from jarvis_api.jobs.sync_itmo import JOB_NAME, run_once, sync_window

КЛЮЧ = base64.b64encode(bytes(32)).decode()
ПРОВАЙДЕР = "https://id.itmo.test/auth/realms/itmo"
ПОРТАЛ = "https://my.itmo.test/api"

# Пятница той же недели, что и фикстура: пары 7 и 9 сентября попадают в окно.
СЕЙЧАС = dt.datetime(2026, 9, 5, 4, 0, tzinfo=dt.UTC)


def настройки(**переопределения: object) -> Settings:
    основа: dict[str, object] = {
        "isu_login": "x000000",
        "isu_password": "не настоящий пароль",
        "isu_cred_key": КЛЮЧ,
        "itmo_auth_provider_url": ПРОВАЙДЕР,
        "itmo_api_base_url": ПОРТАЛ,
        # Без пауз: повторы проверяются отдельно, а спать в тестах незачем.
        "itmo_retry_backoff_seconds": 0.0,
    }
    основа.update(переопределения)
    return Settings(**основа)  # type: ignore[arg-type]


class ПодставнойПортал:
    """ITMO ID и my.itmo.ru в одном обработчике.

    Вход всегда успешен - его углы проверяет `test_itmo_auth.py`. Здесь
    интересен только ответ расписания и его отказы.
    """

    def __init__(self, расписание: Any) -> None:
        self.расписание = расписание
        self.статус_расписания = 200
        self.сетевой_отказ = False
        self.запросов_расписания = 0
        self.последние_параметры: dict[str, str] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        адрес = str(request.url).split("?")[0]

        if адрес.startswith(ПРОВАЙДЕР):
            if адрес.endswith("/auth"):
                return httpx2.Response(
                    200,
                    text='<script>{"loginAction":"'
                    f'{ПРОВАЙДЕР}/login-actions/authenticate?session_code=abc"}}</script>',
                )
            if адрес.endswith("/authenticate"):
                return httpx2.Response(
                    302,
                    headers={"Location": "https://my.itmo.test/login/callback?code=6f1a-code"},
                )
            return httpx2.Response(
                200,
                json={
                    "access_token": "access-1",
                    "expires_in": 3600,
                    "refresh_token": "refresh-1",
                    "refresh_expires_in": 2592000,
                },
            )

        if адрес == f"{ПОРТАЛ}/schedule/schedule/personal":
            self.запросов_расписания += 1
            self.последние_параметры = dict(request.url.params)
            if self.сетевой_отказ:
                raise httpx2.ConnectError("сеть недоступна", request=request)
            if self.статус_расписания != 200:
                return httpx2.Response(self.статус_расписания, text="Bad Gateway")
            return httpx2.Response(200, json=self.расписание)

        raise AssertionError(f"тест ушёл на неожиданный адрес: {request.url}")


@pytest.fixture
def портал() -> ПодставнойПортал:
    return ПодставнойПортал(загрузить_фикстуру("itmo", "schedule_ok.json"))


@pytest.fixture
def http(портал: ПодставнойПортал) -> Iterator[httpx2.Client]:
    with httpx2.Client(transport=httpx2.MockTransport(портал), follow_redirects=False) as клиент:
        yield клиент


@pytest.fixture
def база_с_настройками(сессия: Session) -> Session:
    """Строка настроек с московской зоной.

    Заводится явно: зона решает, какой датой лягут пары, и молчаливое
    умолчание здесь пряталo бы половину смысла теста.
    """
    сессия.add(Setting(id=1, timezone="Europe/Moscow"))
    сессия.flush()
    return сессия


def пары(session: Session) -> list[ItmoLesson]:
    return list(session.scalars(select(ItmoLesson).order_by(ItmoLesson.starts_at)))


# --- окно -------------------------------------------------------------------


def test_окно_считается_вокруг_сегодняшнего_дня() -> None:
    """Не учебный год целиком, как в референсе: назад неделя, вперёд месяц."""
    начало, конец = sync_window(настройки(), dt.date(2026, 9, 5))

    assert начало == dt.date(2026, 8, 29)
    assert конец == dt.date(2026, 10, 3)


def test_окно_уходит_в_портал_датами_а_не_метками_времени(
    база_с_настройками: Session, http: httpx2.Client, портал: ПодставнойПортал
) -> None:
    """Портал понимает YYYY-MM-DD. Метка времени ему не по формату."""
    run_once(база_с_настройками, настройки(), http, apply=False, now=СЕЙЧАС)

    assert портал.последние_параметры == {
        "date_start": "2026-08-29",
        "date_end": "2026-10-03",
    }


# --- dry-run ----------------------------------------------------------------


def test_dry_run_не_пишет_в_базу_ничего(база_с_настройками: Session, http: httpx2.Client) -> None:
    """`CLAUDE.md`: всё, что пишет наружу, по умолчанию dry-run.

    Здесь «наружу» - это зеркало и журналы. Проверяется буквально, включая
    `audit_log` и `job_runs`: отчёт о том, чего не делали, - тоже запись.
    """
    код = run_once(база_с_настройками, настройки(), http, apply=False, now=СЕЙЧАС)

    assert код == 0
    assert пары(база_с_настройками) == []
    assert база_с_настройками.scalar(select(func.count()).select_from(AuditLogEntry)) == 0
    assert база_с_настройками.scalar(select(func.count()).select_from(JobRun)) == 0


def test_dry_run_всё_же_сохраняет_добытые_токены(
    база_с_настройками: Session, http: httpx2.Client
) -> None:
    """Токены - не результат работы джоба, а плата за доступ к порталу.

    Единственное исключение из «dry-run не пишет ничего», и оно осознанное:
    выбросить добытые токены значило бы входить паролем на каждый ручной
    прогон - ровно то, чего мы избегаем приоритетом refresh-токена.

    Проверяется коммитом, а не состоянием сессии: без него токены пропали бы
    при закрытии сессии, и тест этого бы не заметил.
    """
    run_once(база_с_настройками, настройки(), http, apply=False, now=СЕЙЧАС)
    база_с_настройками.expunge_all()

    виды = set(база_с_настройками.scalars(select(IntegrationToken.kind)))
    assert виды == {"password", "access_token", "refresh_token"}


# --- запись -----------------------------------------------------------------


def test_apply_записывает_зеркало(база_с_настройками: Session, http: httpx2.Client) -> None:
    """Три пары фикстуры ложатся в базу с местной датой и временем в UTC."""
    код = run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)
    строки = пары(база_с_настройками)

    assert код == 0
    assert len(строки) == 3
    assert строки[0].subject == "Дисциплина А"
    assert строки[0].lesson_date == dt.date(2026, 9, 7)
    assert строки[0].starts_at == dt.datetime(2026, 9, 7, 7, 0, tzinfo=dt.UTC)
    assert строки[0].fetched_at == СЕЙЧАС


def test_повторный_прогон_ничего_не_задваивает(
    база_с_настройками: Session, http: httpx2.Client
) -> None:
    """Второй запуск за день - штатный сценарий catch-up (§11.2), не авария.

    Инвариант хоста 5 проверяется здесь буквально: ключ детерминирован,
    значит второй прогон попадает в те же строки.
    """
    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)
    ключи_после_первого = [строка.source_key for строка in пары(база_с_настройками)]

    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)

    assert [строка.source_key for строка in пары(база_с_настройками)] == ключи_после_первого


def test_исчезнувшая_из_расписания_пара_исчезает_из_зеркала(
    база_с_настройками: Session, http: httpx2.Client, портал: ПодставнойПортал
) -> None:
    """Иначе отменённая лекция звонила бы в телефон до конца семестра.

    Reconcile Э4 удаляет из Google то, чего нет в зеркале, - и если зеркало
    хранит отменённое, удалять он будет нечего.
    """
    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)

    урезанное = загрузить_фикстуру("itmo", "schedule_ok.json")
    урезанное["data"][0]["lessons"] = урезанное["data"][0]["lessons"][:1]
    портал.расписание = урезанное
    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)

    assert [строка.subject for строка in пары(база_с_настройками)] == [
        "Дисциплина А",
        "Дисциплина В",
    ]


def test_изменившаяся_пара_обновляется_а_не_добавляется(
    база_с_настройками: Session, http: httpx2.Client, портал: ПодставнойПортал
) -> None:
    """Смена аудитории - та же пара. Ключ не меняется, строка обновляется."""
    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)

    изменённое = загрузить_фикстуру("itmo", "schedule_ok.json")
    изменённое["data"][0]["lessons"][0]["room"] = "4321"
    портал.расписание = изменённое
    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)

    строки = пары(база_с_настройками)
    assert len(строки) == 3
    assert строки[0].room == "4321"


def test_пары_вне_окна_забор_не_трогает(база_с_настройками: Session, http: httpx2.Client) -> None:
    """Окно заменяется целиком, но только окно.

    Прошлогодняя пара - история, а не мусор: по ней считается отставание
    и она уже записана в календарь.
    """
    давняя = ItmoLesson(
        source_key="itmo:2025-09-01:10:00:0000000000000000",
        lesson_date=dt.date(2025, 9, 1),
        starts_at=dt.datetime(2025, 9, 1, 7, 0, tzinfo=dt.UTC),
        ends_at=dt.datetime(2025, 9, 1, 8, 30, tzinfo=dt.UTC),
        subject="Прошлогодняя",
        fetched_at=dt.datetime(2025, 9, 1, 4, 0, tzinfo=dt.UTC),
    )
    база_с_настройками.add(давняя)
    база_с_настройками.flush()

    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)

    assert база_с_настройками.get(ItmoLesson, давняя.source_key) is not None


def test_пустое_расписание_записывается_как_пустое(
    база_с_настройками: Session, http: httpx2.Client, портал: ПодставнойПортал
) -> None:
    """Каникулы - рабочее состояние. Джоб не должен считать их отказом."""
    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)
    портал.расписание = загрузить_фикстуру("itmo", "schedule_empty.json")

    код = run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)

    assert код == 0
    assert пары(база_с_настройками) == []


# --- журналы ----------------------------------------------------------------


def test_успешный_забор_оставляет_след_в_журналах(
    база_с_настройками: Session, http: httpx2.Client
) -> None:
    """`audit_log` отвечает на «почему расписание такое», `job_runs` - на
    «был ли прогон сегодня» (§11.2)."""
    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)

    запись = база_с_настройками.scalars(select(AuditLogEntry)).one()
    прогон = база_с_настройками.scalars(select(JobRun)).one()

    assert (запись.kind, запись.actor, запись.status) == ("itmo_fetch", JOB_NAME, "ok")
    assert запись.detail is not None and запись.detail["added"] == 3
    assert прогон.status == "ok"
    # Дата прогона местная: в UTC ночной забор попал бы во вчера.
    assert прогон.run_date == dt.date(2026, 9, 5)


def test_два_прогона_за_день_дают_одну_строку_в_job_runs(
    база_с_настройками: Session, http: httpx2.Client
) -> None:
    """Ограничение базы делает catch-up безопасным, а не аккуратность кода."""
    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)
    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС + dt.timedelta(hours=6))

    assert база_с_настройками.scalar(select(func.count()).select_from(JobRun)) == 1


# --- отказы -----------------------------------------------------------------


def test_недоступный_портал_зеркало_не_трогает(
    база_с_настройками: Session, http: httpx2.Client, портал: ПодставнойПортал
) -> None:
    """Инвариант 9 и §10: показываем последнее сохранённое с пометкой давности.

    Пометка берётся из `fetched_at`, поэтому он обязан остаться прежним -
    обновлённый на неудачном заборе выдал бы вчерашние данные за сегодняшние.
    """
    run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)
    портал.сетевой_отказ = True

    позже = СЕЙЧАС + dt.timedelta(days=1)
    код = run_once(база_с_настройками, настройки(), http, apply=True, now=позже)

    строки = пары(база_с_настройками)
    assert код == 1
    assert len(строки) == 3
    assert all(строка.fetched_at == СЕЙЧАС for строка in строки)


def test_отказ_портала_повторяется_и_попадает_в_журнал(
    база_с_настройками: Session, http: httpx2.Client, портал: ПодставнойПортал
) -> None:
    """Джоб падает громко: код возврата, запись в `audit_log`, отметка в
    `job_runs`. Смотреть на него в момент запуска некому."""
    портал.статус_расписания = 502

    код = run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)

    assert код == 1
    assert портал.запросов_расписания == 3
    запись = база_с_настройками.scalars(select(AuditLogEntry)).one()
    прогон = база_с_настройками.scalars(select(JobRun)).one()
    assert запись.status == "error"
    assert прогон.status == "failed"
    assert прогон.error is not None and "502" in прогон.error


def test_изменившийся_формат_не_повторяется(
    база_с_настройками: Session, http: httpx2.Client, портал: ПодставнойПортал
) -> None:
    """Повторять нечего: тот же запрос вернёт тот же нечитаемый ответ.

    Три попытки здесь означали бы три минуты ожидания на пустом месте
    и три одинаковые строки в логе.
    """
    портал.расписание = загрузить_фикстуру("itmo", "schedule_broken.json")

    код = run_once(база_с_настройками, настройки(), http, apply=True, now=СЕЙЧАС)

    assert код == 1
    assert портал.запросов_расписания == 1
    запись = база_с_настройками.scalars(select(AuditLogEntry)).one()
    assert запись.detail is not None
    assert "формат ответа my.itmo.ru изменился" in str(запись.detail["error"])


def test_отказ_в_dry_run_журналы_не_пишет(
    база_с_настройками: Session, http: httpx2.Client, портал: ПодставнойПортал
) -> None:
    """dry-run не пишет ничего, включая след об отказе - иначе «ничего не
    записано» перестаёт быть правдой ровно в неудачный день."""
    портал.статус_расписания = 502

    код = run_once(база_с_настройками, настройки(), http, apply=False, now=СЕЙЧАС)

    assert код == 1
    assert база_с_настройками.scalar(select(func.count()).select_from(AuditLogEntry)) == 0
    assert база_с_настройками.scalar(select(func.count()).select_from(JobRun)) == 0


def test_неизвестная_зона_в_настройках_роняет_забор(сессия: Session, http: httpx2.Client) -> None:
    """Подставить UTC значило бы сдвинуть всё расписание на три часа молча."""
    сессия.add(Setting(id=1, timezone="Марс/Олимп"))
    сессия.flush()

    код = run_once(сессия, настройки(), http, apply=True, now=СЕЙЧАС)

    assert код == 1
    прогон = сессия.scalars(select(JobRun)).one()
    assert прогон.status == "failed"
    assert "неизвестная зона" in (прогон.error or "")


def test_пустой_ключ_шифрования_роняет_забор_до_запроса(
    база_с_настройками: Session, http: httpx2.Client, портал: ПодставнойПортал
) -> None:
    """Незаполненный ISU_CRED_KEY - это «шифровать нечем».

    Положить пароль в базу открытым текстом вместо отказа означало бы
    отдать его в первый же ночной дамп.
    """
    код = run_once(база_с_настройками, настройки(isu_cred_key=""), http, apply=True, now=СЕЙЧАС)

    assert код == 1
    assert портал.запросов_расписания == 0
    запись = база_с_настройками.scalars(select(AuditLogEntry)).one()
    assert "ISU_CRED_KEY пуст" in str(запись.detail["error"] if запись.detail else "")


def test_без_строки_настроек_берётся_московская_зона(сессия: Session, http: httpx2.Client) -> None:
    """Строку настроек ещё никто не создавал - это рабочее состояние.

    Умолчание совпадает с `server_default` колонки: два разных умолчания
    дали бы расписание, сдвинутое на часы, в зависимости от того, успел ли
    кто-нибудь завести строку.
    """
    код = run_once(сессия, настройки(), http, apply=True, now=СЕЙЧАС)

    assert код == 0
    assert пары(сессия)[0].starts_at == dt.datetime(2026, 9, 7, 7, 0, tzinfo=dt.UTC)
