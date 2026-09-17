"""Разбор выписок банков: реестр адаптеров, по одному на банк (ADR-030).

Канал книжки один - файл, выгруженный owner руками (ADR-024). Официального
API для физлиц у банков нет, автоматического канала нет и не планируется,
поэтому здесь нет ни клиентов, ни сети: пакет превращает байты в строки
и ничего больше.

**Формат файла - дело адаптера** (ADR-044): Т-Банк отдаёт CSV, Ozon Bank -
PDF, и ядро импорта об этом не знает. Общее у них одно: банк опознаётся
по содержимому файла, а не по его имени и не выбором owner.

Схема выгрузки **неофициальная**, ровно как у my.itmo.ru: банк вправе
изменить состав колонок без предупреждения. Отсюда главное правило пакета -
**отказ целиком**, с указанием колонки или строки, а не разбор того,
что получилось. Молча пропущенная колонка суммы даёт правдоподобную,
но неверную книжку, а обнаруживается это через месяцы и не лечится
переимпортом.

Формат каждого банка фиксируется по настоящей выгрузке: нет файла - нет
адаптера, и выдуманных колонок в коде тоже нет (§15.1).
"""

from jarvis_api.integrations.statements.base import (
    BankAdapter,
    ColumnMismatchError,
    CsvBankAdapter,
    ParsedStatement,
    RowError,
    StatementError,
    StatementRow,
    TotalsMismatchError,
    UnknownBankError,
)
from jarvis_api.integrations.statements.registry import АДАПТЕРЫ, parse_statement

__all__ = [
    "BankAdapter",
    "ColumnMismatchError",
    "CsvBankAdapter",
    "ParsedStatement",
    "RowError",
    "StatementError",
    "StatementRow",
    "TotalsMismatchError",
    "UnknownBankError",
    "parse_statement",
    "АДАПТЕРЫ",
]
