# import os
# import logging
# from pathlib import Path
# from datetime import datetime
# from dotenv import load_dotenv
# import mssql_python
# from apscheduler.schedulers.blocking import BlockingScheduler
#
# # --- Логирование в файл и консоль ---
# BASE_DIR = Path(__file__).resolve().parent
# LOG_FILE = BASE_DIR / "update_app.log"
#
# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s [%(levelname)s] %(message)s",
#     handlers=[
#         logging.FileHandler(LOG_FILE, encoding="utf-8"),
#         logging.StreamHandler(),
#     ],
# )
# log = logging.getLogger(__name__)
#
# # --- Загрузка .env ---
# load_dotenv(BASE_DIR / ".env")
#
#
# def run_procedures():
#     connection_string = os.getenv("SQL_CONNECTION_STRING")
#     if not connection_string:
#         log.error("Переменная SQL_CONNECTION_STRING не найдена в .env")
#         return
#
#     # Текущий год, месяц и квартал
#     now = datetime.now()
#     year = now.year
#     month = now.month
#     current_quarter = (now.month - 1) // 3 + 1  # 1..4
#
#     conn = None
#     cursor = None
#     try:
#         conn = mssql_python.connect(connection_string)
#         cursor = conn.cursor()
#
#         # ===== Процедура 1: usp_RefreshReportPlanFact (Year, Month) =====
#         log.info("СТАРТ usp_RefreshReportPlanFact (Year=%s, Month=%s)", year, month)
#         t1 = datetime.now()
#         cursor.execute(
#             "{CALL dbo.usp_RefreshReportPlanFact (?, ?)}",
#             (year, month),
#         )
#         conn.commit()
#         log.info(
#             "ЗАВЕРШЕНО usp_RefreshReportPlanFact за %.2f сек",
#             (datetime.now() - t1).total_seconds(),
#         )
#
#         # ===== Процедура 2: usp_RefreshReportTerritory (Year, Quarter) — только текущий квартал =====
#         log.info(
#             "СТАРТ обновления dbo.ReportTerritory (Year=%s, Quarter=%s)",
#             year, current_quarter,
#         )
#         t2 = datetime.now()
#         cursor.execute(
#             "{CALL dbo.usp_RefreshReportTerritory (?, ?)}",
#             (year, current_quarter),
#         )
#         conn.commit()
#         log.info(
#             "ЗАВЕРШЕНО обновление dbo.ReportTerritory за %.2f сек",
#             (datetime.now() - t2).total_seconds(),
#         )
#
#         log.info("Все задачи выполнены успешно.")
#
#     except Exception:
#         log.exception("Ошибка при выполнении задач")
#         if conn:
#             try:
#                 cursor.execute("IF @@TRANCOUNT > 0 ROLLBACK;")
#                 conn.commit()
#                 log.info("Выполнен ROLLBACK незавершённой транзакции.")
#             except Exception:
#                 log.exception("Не удалось выполнить ROLLBACK")
#     finally:
#         if cursor:
#             cursor.close()
#         if conn:
#             conn.close()
#
#
# if __name__ == "__main__":
#     scheduler = BlockingScheduler()
#     scheduler.add_job(
#         run_procedures,
#         "interval",
#         hours = 1,
#         # minutes=1,
#         max_instances=1,
#         coalesce=True,
#         misfire_grace_time=300,
#     )
#     log.info("Планировщик запущен. Ожидание выполнения...")
#     try:
#         scheduler.start()
#     except (KeyboardInterrupt, SystemExit):
#         log.info("Планировщик остановлен.")

import os
import logging
import signal
from pathlib import Path
from datetime import datetime, date
from calendar import monthrange

import requests
from dotenv import load_dotenv
import mssql_python
from apscheduler.schedulers.blocking import BlockingScheduler


# ==========================================================
# Логирование
# ==========================================================
BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "update_app.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

load_dotenv(BASE_DIR / ".env")


# ==========================================================
# Константы поликлиники
# ==========================================================
VID_MED_POMOSCHI = "Поликлиника"
VOMC_TYPE        = "Приемы"
API_BASE         = "http://1cserver.orthobarnaul.ru:81/polyclinic/hs/consent/priemy"


# ==========================================================
# API 1С
# ==========================================================
def api_get_count(date_from: date, date_to: date) -> int:
    """Возвращает число приёмов из API (последний числовой токен)."""
    url = f"{API_BASE}?dateFrom={date_from:%Y-%m-%d}&dateTo={date_to:%Y-%m-%d}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    tokens = r.text.split()
    for tok in reversed(tokens):
        try:
            return int(tok)
        except ValueError:
            continue
    raise ValueError(f"Не удалось извлечь число из ответа API: {r.text!r}")


def period_dates(year: int, month: int, today: date):
    start = date(year, month, 1)
    if (year, month) == (today.year, today.month):
        end = today
    else:
        end = date(year, month, monthrange(year, month)[1])
    return start, end


def get_fact_month(year: int, month: int, today: date) -> int:
    s, e = period_dates(year, month, today)
    return api_get_count(s, e)


def get_fact_ytd(year: int, month: int, today: date) -> int:
    _, e = period_dates(year, month, today)
    return api_get_count(date(year, 1, 1), e)


# ==========================================================
# Планы из VOMC
# ==========================================================
def get_plan_year(cursor, year: int) -> int:
    cursor.execute("""
        SELECT SUM(Quantity)
        FROM VOMC
        WHERE TypesOfMedicalCare = ? AND YYYY = ?
    """, (VOMC_TYPE, year))
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def get_plan_month(cursor, year: int, month: int) -> int:
    cursor.execute("""
        SELECT SUM(Quantity)
        FROM VOMC
        WHERE TypesOfMedicalCare = ? AND YYYY = ? AND MM = ?
    """, (VOMC_TYPE, year, month))
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def get_prev_rasch_ytd(cursor, year: int, prev_month: int) -> int:
    cursor.execute("""
        SELECT RaschozhdenieSNachala2026
        FROM dbo.ReportPlanFact
        WHERE PeriodYear = ? AND PeriodMonth = ? AND VidMedPomoschi = ?
    """, (year, prev_month, VID_MED_POMOSCHI))
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# ==========================================================
# UPSERT
# ==========================================================
MERGE_SQL = """
MERGE dbo.ReportPlanFact AS tgt
USING (SELECT ? AS PeriodYear, ? AS PeriodMonth, ? AS VidMedPomoschi) AS src
ON  tgt.PeriodYear     = src.PeriodYear
AND tgt.PeriodMonth    = src.PeriodMonth
AND tgt.VidMedPomoschi = src.VidMedPomoschi
WHEN MATCHED THEN UPDATE SET
     PlanTekushchiyMes               = ?,
     DolzhnoBytVypisano              = ?,
     FaktTekushchiyMes               = ?,
     RaschozhdenieAbsTekushchiyMes   = ?,
     PercentIspolneniyaTekushchiyMes = ?,
     RaschozhdenieSNachala2026       = ?,
     PlanNa2026                      = ?,
     Ispolneno2026                   = ?,
     PercentIspolneniya2026          = ?,
     OstatokObemovDoKonca2026        = ?,
     UpdatedAt                       = DATEADD(HOUR, 7, SYSUTCDATETIME())
WHEN NOT MATCHED BY TARGET THEN INSERT
     (PeriodYear, PeriodMonth, VidMedPomoschi,
      PlanTekushchiyMes, DolzhnoBytVypisano, FaktTekushchiyMes,
      RaschozhdenieAbsTekushchiyMes, PercentIspolneniyaTekushchiyMes,
      RaschozhdenieSNachala2026,
      PlanNa2026, Ispolneno2026, PercentIspolneniya2026, OstatokObemovDoKonca2026,
      UpdatedAt)
VALUES
     (?, ?, ?,
      ?, ?, ?,
      ?, ?,
      ?,
      ?, ?, ?, ?,
      DATEADD(HOUR, 7, SYSUTCDATETIME()));
"""


def upsert_row(cursor, *, year, month, plan_m, dolzhno, fact_m,
               rasch_abs_m, pct_m, rasch_ytd,
               plan_y, isp_y, pct_y, ostatok):
    cursor.execute(
        MERGE_SQL,
        # USING
        (year, month, VID_MED_POMOSCHI,
         # UPDATE
         plan_m, dolzhno, fact_m, rasch_abs_m, pct_m, rasch_ytd,
         plan_y, isp_y, pct_y, ostatok,
         # INSERT
         year, month, VID_MED_POMOSCHI,
         plan_m, dolzhno, fact_m, rasch_abs_m, pct_m, rasch_ytd,
         plan_y, isp_y, pct_y, ostatok),
    )


# ==========================================================
# Поликлиника: заполнение ReportPlanFact
# ==========================================================
def refresh_poliklinika(cursor, today: date):
    year  = today.year
    month = today.month

    plan_year  = get_plan_year(cursor, year)
    plan_month = get_plan_month(cursor, year, month)

    fact_month = get_fact_month(year, month, today)
    isp_ytd    = get_fact_ytd(year, month, today)

    total_days   = monthrange(year, month)[1]
    elapsed_days = today.day
    dolzhno      = int(plan_month / total_days * elapsed_days) if total_days else 0

    rasch_abs_month = fact_month - dolzhno
    pct_month       = (fact_month / plan_month * 100.0) if plan_month else 0.0

    # Накопление берём из БД за предыдущий месяц (если январь — 0)
    prev_rasch_ytd = get_prev_rasch_ytd(cursor, year, month - 1) if month > 1 else 0
    rasch_ytd      = prev_rasch_ytd + rasch_abs_month

    pct_year = (isp_ytd / plan_year * 100.0) if plan_year else 0.0
    ostatok  = plan_year - isp_ytd

    upsert_row(
        cursor,
        year=year, month=month,
        plan_m=plan_month, dolzhno=dolzhno, fact_m=fact_month,
        rasch_abs_m=rasch_abs_month, pct_m=pct_month,
        rasch_ytd=rasch_ytd,
        plan_y=plan_year, isp_y=isp_ytd,
        pct_y=pct_year, ostatok=ostatok,
    )
    log.info(
        "Поликлиника %s-%02d: план=%s, должно=%s, факт=%s, Δмес=%s, ΔYTD=%s, "
        "%%мес=%.2f, исп.год=%s/%s, %%год=%.2f, остаток=%s",
        year, month, plan_month, dolzhno, fact_month,
        rasch_abs_month, rasch_ytd, pct_month,
        isp_ytd, plan_year, pct_year, ostatok,
    )


# ==========================================================
# Главная задача: поликлиника + хранимые процедуры
# ==========================================================
def run_procedures():
    connection_string = os.getenv("SQL_CONNECTION_STRING")
    if not connection_string:
        log.error("Переменная SQL_CONNECTION_STRING не найдена в .env")
        return

    now = datetime.now()
    year = now.year
    month = now.month
    current_quarter = (now.month - 1) // 3 + 1

    conn = None
    cursor = None
    try:
        conn = mssql_python.connect(connection_string)
        cursor = conn.cursor()

        # ===== 1. Поликлиника: ReportPlanFact (API + VOMC + MERGE) =====
        log.info("СТАРТ обновления dbo.ReportPlanFact (Поликлиника)")
        t0 = datetime.now()
        refresh_poliklinika(cursor, today=date.today())
        conn.commit()
        log.info(
            "ЗАВЕРШЕНО обновление dbo.ReportPlanFact (Поликлиника) за %.2f сек",
            (datetime.now() - t0).total_seconds(),
        )

        # ===== 2. Процедура 1: usp_RefreshReportPlanFact =====
        log.info("СТАРТ usp_RefreshReportPlanFact (Year=%s, Month=%s)", year, month)
        t1 = datetime.now()
        cursor.execute("{CALL dbo.usp_RefreshReportPlanFact (?, ?)}", (year, month))
        conn.commit()
        log.info(
            "ЗАВЕРШЕНО usp_RefreshReportPlanFact за %.2f сек",
            (datetime.now() - t1).total_seconds(),
        )

        # ===== 3. Процедура 2: usp_RefreshReportTerritory (текущий квартал) =====
        log.info("СТАРТ dbo.ReportTerritory (Year=%s, Quarter=%s)", year, current_quarter)
        t2 = datetime.now()
        cursor.execute(
            "{CALL dbo.usp_RefreshReportTerritory (?, ?)}",
            (year, current_quarter),
        )
        conn.commit()
        log.info(
            "ЗАВЕРШЕНО dbo.ReportTerritory за %.2f сек",
            (datetime.now() - t2).total_seconds(),
        )

        log.info("Все задачи выполнены успешно.")

    except Exception:
        log.exception("Ошибка при выполнении задач")
        if conn:
            try:
                cursor.execute("IF @@TRANCOUNT > 0 ROLLBACK;")
                conn.commit()
                log.info("Выполнен ROLLBACK незавершённой транзакции.")
            except Exception:
                log.exception("Не удалось выполнить ROLLBACK")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ==========================================================
# Планировщик
# ==========================================================
if __name__ == "__main__":
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_procedures,
        "interval",
        hours=1,
        # minutes=1,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    log.info("Планировщик запущен. Ожидание выполнения...")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Планировщик остановлен.")

# scheduler.start()