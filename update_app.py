import os
import logging
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
import mssql_python
from apscheduler.schedulers.blocking import BlockingScheduler

# --- Логирование в файл и консоль ---
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

# --- Загрузка .env ---
load_dotenv(BASE_DIR / ".env")


def run_procedures():
    connection_string = os.getenv("SQL_CONNECTION_STRING")
    if not connection_string:
        log.error("Переменная SQL_CONNECTION_STRING не найдена в .env")
        return

    # Текущий год, месяц и квартал
    now = datetime.now()
    year = now.year
    month = now.month
    current_quarter = (now.month - 1) // 3 + 1  # 1..4

    conn = None
    cursor = None
    try:
        conn = mssql_python.connect(connection_string)
        cursor = conn.cursor()

        # ===== Процедура 1: usp_RefreshReportPlanFact (Year, Month) =====
        log.info("СТАРТ usp_RefreshReportPlanFact (Year=%s, Month=%s)", year, month)
        t1 = datetime.now()
        cursor.execute(
            "{CALL dbo.usp_RefreshReportPlanFact (?, ?)}",
            (year, month),
        )
        conn.commit()
        log.info(
            "ЗАВЕРШЕНО usp_RefreshReportPlanFact за %.2f сек",
            (datetime.now() - t1).total_seconds(),
        )

        # ===== Процедура 2: usp_RefreshReportTerritory (Year, Quarter) — только текущий квартал =====
        log.info(
            "СТАРТ обновления dbo.ReportTerritory (Year=%s, Quarter=%s)",
            year, current_quarter,
        )
        t2 = datetime.now()
        cursor.execute(
            "{CALL dbo.usp_RefreshReportTerritory (?, ?)}",
            (year, current_quarter),
        )
        conn.commit()
        log.info(
            "ЗАВЕРШЕНО обновление dbo.ReportTerritory за %.2f сек",
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


if __name__ == "__main__":
    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_procedures,
        "interval",
        hours = 1,
        # minutes=1,
        max_instances=1,
        coalesce=True,
    )
    log.info("Планировщик запущен. Ожидание выполнения...")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Планировщик остановлен.")