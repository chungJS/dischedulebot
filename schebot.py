import os

import discord
from discord import app_commands
from discord.ext import commands

import sqlite3
import logging
import traceback

from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo


# ============================================================
# 기본 설정
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = 1540910310536388638

DB_PATH = "schedule.db"

# 특정 날짜 일정은 오래된 데이터를 자동 정리합니다.
SCHEDULE_RETENTION_DAYS = 10

# 한국 시간
KST = ZoneInfo("Asia/Seoul")


# ============================================================
# 사용자 설정
#
# mask:
# 개 = 1
# 소 = 2
# 주 = 4
# ============================================================

USER_INFO = {
    1540937388061368413: {
        "mask": 1,
        "name": "개리길이",
        "emoji": "🟥",
    },
    1540937637836365935: {
        "mask": 2,
        "name": "소벌도리",
        "emoji": "🟦",
    },
    363271839776243714: {
        "mask": 4,
        "name": "주말을월일로",
        "emoji": "🟨",
    },
}

# 사용자 ID -> mask
USER_MASKS = {
    user_id: info["mask"]
    for user_id, info in USER_INFO.items()
}

# mask -> 사용자 정보
MASK_USER_INFO = {
    info["mask"]: info
    for info in USER_INFO.values()
}


# ============================================================
# 로깅
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

logger = logging.getLogger("schedule_bot")


# ============================================================
# 시간 관련 함수
# ============================================================

def now_kst() -> datetime:
    """현재 한국 시간을 반환합니다."""
    return datetime.now(KST)


def today_kst() -> date:
    """현재 한국 날짜를 반환합니다."""
    return now_kst().date()


# ============================================================
# DB
# ============================================================

def get_db():
    """
    DB 연결을 생성합니다.

    전역 connection 하나를 계속 사용하는 대신
    필요한 작업마다 새로운 연결을 사용합니다.
    """
    conn = sqlite3.connect(
        DB_PATH,
        timeout=10
    )
    conn.row_factory = sqlite3.Row

    # 외래키 기능 활성화
    conn.execute("PRAGMA foreign_keys = ON")

    return conn


def ensure_column(conn, table_name: str, column_name: str, column_definition: str):
    """
    기존 DB에 컬럼이 없는 경우 컬럼을 추가합니다.
    """
    columns = conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()

    existing_columns = {
        row["name"]
        for row in columns
    }

    if column_name not in existing_columns:
        conn.execute(
            f"ALTER TABLE {table_name} "
            f"ADD COLUMN {column_name} {column_definition}"
        )


def init_db():
    """DB 테이블을 생성하고 기본 검사를 수행합니다."""

    with get_db() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS specific_schedule (
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                busy_mask INTEGER NOT NULL DEFAULT 0,
                free_mask INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date, hour),
                CHECK (hour >= 0 AND hour <= 23),
                CHECK (busy_mask >= 0),
                CHECK (free_mask >= 0)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS recurring_schedule (
                day_of_week INTEGER NOT NULL,
                hour INTEGER NOT NULL,
                busy_mask INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day_of_week, hour),
                CHECK (day_of_week >= 0 AND day_of_week <= 6),
                CHECK (hour >= 0 AND hour <= 23),
                CHECK (busy_mask >= 0)
            )
        """)

        # 기존 DB가 예전 구조였을 경우 보완
        ensure_column(
            conn,
            "specific_schedule",
            "free_mask",
            "INTEGER NOT NULL DEFAULT 0"
        )

        # 조회 성능 보조용 인덱스
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_specific_date
            ON specific_schedule(date)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_recurring_day_hour
            ON recurring_schedule(day_of_week, hour)
        """)

        conn.commit()


def cleanup_old_schedule():
    """
    오래된 특정 날짜 일정 삭제.

    반복 일정은 삭제하지 않습니다.
    """
    cutoff = today_kst() - timedelta(days=SCHEDULE_RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    with get_db() as conn:
        conn.execute(
            """
            DELETE FROM specific_schedule
            WHERE date < ?
            """,
            (cutoff_str,)
        )
        conn.commit()

    logger.info(
        "오래된 특정 일정 정리 완료: %s 이전",
        cutoff_str
    )


# DB 최초 생성
init_db()
cleanup_old_schedule()


# ============================================================
# 봇
# ============================================================

class ScheduleBot(commands.Bot):

    def __init__(self):
        intents = discord.Intents.default()

        super().__init__(
            command_prefix="!",
            intents=intents
        )

        self.MY_GUILD = discord.Object(
            id=GUILD_ID
        )

    async def setup_hook(self):
        # 테스트 서버에는 즉시 동기화
        self.tree.copy_global_to(
            guild=self.MY_GUILD
        )

        await self.tree.sync(
            guild=self.MY_GUILD
        )

        logger.info(
            "슬래시 명령어 동기화 완료"
        )


bot = ScheduleBot()


# ============================================================
# 공통 메시지
# ============================================================

def send_error_message(
    interaction: discord.Interaction,
    message: str
):
    """
    interaction 응답 여부에 따라
    response 또는 followup으로 메시지를 보냅니다.
    """
    return (
        interaction.followup.send(
            message,
            ephemeral=True
        )
        if interaction.response.is_done()
        else interaction.response.send_message(
            message,
            ephemeral=True
        )
    )


# ============================================================
# 사용자 / 마스크
# ============================================================

def get_user_mask(user_id: int):
    """사용자의 mask를 반환합니다."""
    return USER_MASKS.get(user_id)


def get_emoji(mask: int) -> str:
    """
    일정 mask를 종합 색상으로 변환합니다.
    """

    color_map = {
        0: "⬜",
        1: "🟥",
        2: "🟦",
        3: "🟪",
        4: "🟨",
        5: "🟧",
        6: "🟩",
        7: "⬛"
    }

    return color_map.get(mask, "⬜")


def get_users_from_mask(mask: int) -> str:
    """
    mask를 분석해서 불가능한 사용자 이름을 반환합니다.

    규칙:
    - 1명: 전체 이름
    - 2명 이상: 각 이름의 첫 글자만 연결
      예) 개리길이 + 소벌도리 → 개소
          개리길이 + 주말을월일로 → 개주
          소벌도리 + 주말을월일로 → 소주
          3명 모두 → 개소주
    """

    users = []

    for info in MASK_USER_INFO.values():

        if mask & info["mask"]:
            users.append(info["name"])

    if not users:
        return "전원 가능"

    if len(users) == 1:
        return users[0]

    return "".join(user[0] for user in users)


def get_individual_emojis(mask: int) -> str:
    """
    상세 스케줄용 A/B/C 3칸을 반환합니다.
    """

    result = []

    for info in MASK_USER_INFO.values():

        if mask & info["mask"]:
            result.append(info["emoji"])
        else:
            result.append("⬜")

    return "".join(result)


# ============================================================
# 날짜 파싱
# ============================================================

def parse_user_date(date_str: str) -> datetime:
    """
    사용자가 입력한 M/D 날짜를 datetime으로 변환합니다.

    규칙:
    - 현재 연도를 먼저 사용
    - 이미 지난 날짜면 다음 연도로 처리
    """

    cleaned = date_str.strip().replace(" ", "")

    try:
        month, day = map(
            int,
            cleaned.split("/")
        )
    except (ValueError, AttributeError):
        raise ValueError

    current = now_kst()

    candidate = datetime(
        current.year,
        month,
        day,
        tzinfo=KST
    )

    # 현재 날짜보다 과거라면 다음 해
    if candidate.date() < current.date():
        candidate = candidate.replace(
            year=current.year + 1
        )

    return candidate


# ============================================================
# 시간 검증
# ============================================================

def validate_hours(
    start_hour: int,
    end_hour: int
) -> bool:

    return (
        0 <= start_hour <= 23
        and 1 <= end_hour <= 24
        and start_hour < end_hour
    )


# ============================================================
# 요일 파싱
# ============================================================

DAY_MAP = {
    "월": 0,
    "화": 1,
    "수": 2,
    "목": 3,
    "금": 4,
    "토": 5,
    "일": 6,
}

SPECIAL_DAYS = {
    "매일": [0, 1, 2, 3, 4, 5, 6],
    "평일": [0, 1, 2, 3, 4],
    "주말": [5, 6],
}


def parse_days(days_str: str) -> list[int]:
    """
    사용자가 입력한 요일을 0(월)~6(일) 형태로 변환합니다.

    허용 예:
    매일
    평일
    주말
    월수금
    화,목
    월 화 금

    잘못된 문자가 포함되면 빈 리스트를 반환합니다.
    """

    cleaned = (
        days_str
        .replace(" ", "")
        .replace(",", "")
        .strip()
    )

    if not cleaned:
        return []

    # 매일 / 평일 / 주말은 정확히 일치해야 함
    if cleaned in SPECIAL_DAYS:
        return SPECIAL_DAYS[cleaned].copy()

    # 한 글자씩 모두 요일인지 검사
    if any(char not in DAY_MAP for char in cleaned):
        return []

    return sorted(
        set(
            DAY_MAP[char]
            for char in cleaned
        )
    )


def get_day_names(day_ints: list[int]) -> str:

    selected = set(day_ints)

    if selected == {
        0, 1, 2, 3, 4, 5, 6
    }:
        return "매일"

    if selected == {
        0, 1, 2, 3, 4
    }:
        return "평일"

    if selected == {
        5, 6
    }:
        return "주말"

    names = [
        "월",
        "화",
        "수",
        "목",
        "금",
        "토",
        "일"
    ]

    return ", ".join(
        names[i]
        for i in sorted(selected)
    )


# ============================================================
# 일정 DB 조회
# ============================================================

def load_specific_schedule(
    conn,
    dates: list[date]
):
    """
    지정된 여러 날짜의 특정 일정들을 한 번에 가져옵니다.

    기존에는 시간 하나마다 SELECT했지만,
    지금은 기간 전체를 한 번에 가져옵니다.
    """

    if not dates:
        return {}

    start_date = min(dates)
    end_date = max(dates)

    rows = conn.execute(
        """
        SELECT date, hour, busy_mask, free_mask
        FROM specific_schedule
        WHERE date BETWEEN ? AND ?
        """,
        (
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d")
        )
    ).fetchall()

    return {
        (
            row["date"],
            row["hour"]
        ): (
            row["busy_mask"],
            row["free_mask"]
        )
        for row in rows
    }


def load_recurring_schedule(conn):
    """
    모든 반복 일정을 한 번만 가져옵니다.
    """

    rows = conn.execute(
        """
        SELECT day_of_week, hour, busy_mask
        FROM recurring_schedule
        """
    ).fetchall()

    return {
        (
            row["day_of_week"],
            row["hour"]
        ): row["busy_mask"]
        for row in rows
    }


def get_final_mask(
    target_date: date,
    hour: int,
    specific_map,
    recurring_map
) -> int:
    """
    특정 날짜 + 시간을 기준으로
    최종 불가능 인원을 계산합니다.

    최종 규칙:

    (특정날짜 불가 OR 고정 불가)
    AND
    특정날짜 예외가 아닌 사람
    """

    date_str = target_date.strftime(
        "%Y-%m-%d"
    )

    s_busy, s_free = specific_map.get(
        (date_str, hour),
        (0, 0)
    )

    r_busy = recurring_map.get(
        (target_date.weekday(), hour),
        0
    )

    return (s_busy | r_busy) & ~s_free


# ============================================================
# /일정
# ============================================================

@bot.tree.command(name="일정", description="특정 날짜에 참여 불가능한 시간을 등록합니다.")
@app_commands.describe(
    date="날짜 (예: 10/20)",
    start_hour="시작 시간 (0~23)",
    end_hour="종료 시간 (1~24)"
)
async def add_busy(
    interaction: discord.Interaction,
    date: str,
    start_hour: app_commands.Range[int, 0, 23],
    end_hour: app_commands.Range[int, 1, 24]
):

    user_mask = get_user_mask(
        interaction.user.id
    )

    if user_mask is None:
        return await interaction.response.send_message(
            "❌ 권한이 없습니다.",
            ephemeral=True
        )

    if not validate_hours(
        start_hour,
        end_hour
    ):
        return await interaction.response.send_message(
            "❌ 시간 설정이 올바르지 않습니다.",
            ephemeral=True
        )

    try:
        parsed_date = parse_user_date(date)
    except ValueError:
        return await interaction.response.send_message(
            "❌ 날짜 형식이 올바르지 않습니다. "
            "'8/23' 형태로 입력해주세요.",
            ephemeral=True
        )

    db_date_str = parsed_date.strftime(
        "%Y-%m-%d"
    )

    try:

        with get_db() as conn:

            for hour in range(
                start_hour,
                end_hour
            ):

                row = conn.execute(
                    """
                    SELECT busy_mask, free_mask
                    FROM specific_schedule
                    WHERE date=? AND hour=?
                    """,
                    (
                        db_date_str,
                        hour
                    )
                ).fetchone()

                current_busy = (
                    row["busy_mask"]
                    if row
                    else 0
                )

                current_free = (
                    row["free_mask"]
                    if row
                    else 0
                )

                # 일정 등록 → 불가 추가
                new_busy = (
                    current_busy
                    | user_mask
                )

                # 직접 불가 등록했으므로
                # 해당 사용자의 예외 상태 제거
                new_free = (
                    current_free
                    & ~user_mask
                )

                conn.execute(
                    """
                    INSERT INTO specific_schedule
                        (date, hour, busy_mask, free_mask)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(date, hour)
                    DO UPDATE SET
                        busy_mask=excluded.busy_mask,
                        free_mask=excluded.free_mask
                    """,
                    (
                        db_date_str,
                        hour,
                        new_busy,
                        new_free
                    )
                )

            conn.commit()

    except sqlite3.Error:
        logger.exception(
            "특정 일정 등록 중 DB 오류"
        )

        return await interaction.response.send_message(
            "❌ DB 오류로 일정 등록에 실패했습니다.",
            ephemeral=True
        )

    await interaction.response.send_message(
        f"✅ {parsed_date.strftime('%m/%d')} "
        f"{start_hour}시~{end_hour}시 "
        f"불가 일정 등록 완료"
    )


# ============================================================
# /일정취소
# ============================================================

@bot.tree.command(name="일정취소", description="등록했던 불가 일정을 취소합니다.")
@app_commands.describe(
    date="날짜 (예: 10/20)",
    start_hour="시작 시간 (0~23)",
    end_hour="종료 시간 (1~24)"
)
async def cancel_busy(
    interaction: discord.Interaction,
    date: str,
    start_hour: app_commands.Range[int, 0, 23],
    end_hour: app_commands.Range[int, 1, 24]
):

    user_mask = get_user_mask(
        interaction.user.id
    )

    if user_mask is None:
        return await interaction.response.send_message(
            "❌ 권한이 없습니다.",
            ephemeral=True
        )

    if not validate_hours(
        start_hour,
        end_hour
    ):
        return await interaction.response.send_message(
            "❌ 시간 설정이 올바르지 않습니다.",
            ephemeral=True
        )

    try:
        parsed_date = parse_user_date(date)
    except ValueError:
        return await interaction.response.send_message(
            "❌ 날짜 형식이 올바르지 않습니다. "
            "'10/20' 형태로 입력해주세요.",
            ephemeral=True
        )

    db_date_str = parsed_date.strftime(
        "%Y-%m-%d"
    )

    try:

        with get_db() as conn:

            for hour in range(
                start_hour,
                end_hour
            ):

                row = conn.execute(
                    """
                    SELECT busy_mask, free_mask
                    FROM specific_schedule
                    WHERE date=? AND hour=?
                    """,
                    (
                        db_date_str,
                        hour
                    )
                ).fetchone()

                current_busy = (
                    row["busy_mask"]
                    if row
                    else 0
                )

                current_free = (
                    row["free_mask"]
                    if row
                    else 0
                )

                # 특정 날짜에서 본인의 불가 제거
                new_busy = (
                    current_busy
                    & ~user_mask
                )

                # ------------------------------------------------
                # free_mask 개선
                #
                # 실제로 해당 날짜/시간에
                # 반복 일정이 본인에게 존재할 때만
                # 예외(free_mask)를 추가합니다.
                #
                # 따라서 단순히 /일정취소했다고 해서
                # 앞으로 추가될 고정 일정까지
                # 영구적으로 무시하지 않습니다.
                # ------------------------------------------------

                recurring_row = conn.execute(
                    """
                    SELECT busy_mask
                    FROM recurring_schedule
                    WHERE day_of_week=? AND hour=?
                    """,
                    (
                        parsed_date.weekday(),
                        hour
                    )
                ).fetchone()

                recurring_busy = (
                    recurring_row["busy_mask"]
                    if recurring_row
                    else 0
                )

                if recurring_busy & user_mask:
                    new_free = (
                        current_free
                        | user_mask
                    )
                else:
                    # 반복 일정이 없으면
                    # 기존의 해당 사용자 예외도 제거
                    new_free = (
                        current_free
                        & ~user_mask
                    )

                if (
                    new_busy == 0
                    and new_free == 0
                ):
                    conn.execute(
                        """
                        DELETE FROM specific_schedule
                        WHERE date=? AND hour=?
                        """,
                        (
                            db_date_str,
                            hour
                        )
                    )

                else:

                    conn.execute(
                        """
                        INSERT INTO specific_schedule
                            (date, hour, busy_mask, free_mask)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(date, hour)
                        DO UPDATE SET
                            busy_mask=excluded.busy_mask,
                            free_mask=excluded.free_mask
                        """,
                        (
                            db_date_str,
                            hour,
                            new_busy,
                            new_free
                        )
                    )

            conn.commit()

    except sqlite3.Error:
        logger.exception(
            "특정 일정 취소 중 DB 오류"
        )

        return await interaction.response.send_message(
            "❌ DB 오류로 일정 취소에 실패했습니다.",
            ephemeral=True
        )

    await interaction.response.send_message(
        f"✅ {parsed_date.strftime('%m/%d')} "
        f"{start_hour}시~{end_hour}시 "
        f"불가 일정이 취소되었습니다."
    )


# ============================================================
# /고정
# ============================================================

@bot.tree.command(name="고정", description="매주 반복해서 불가능한 시간을 등록합니다.")
@app_commands.describe(
    days="입력 예: 매일, 평일, 주말, 월수금, 화,목",
    start_hour="시작 시간 (0~23)",
    end_hour="종료 시간 (1~24)"
)
async def add_recurring(
    interaction: discord.Interaction,
    days: str,
    start_hour: app_commands.Range[int, 0, 23],
    end_hour: app_commands.Range[int, 1, 24]
):

    user_mask = get_user_mask(
        interaction.user.id
    )

    if user_mask is None:
        return await interaction.response.send_message(
            "❌ 권한이 없습니다.",
            ephemeral=True
        )

    target_days = parse_days(days)

    if not target_days:
        return await interaction.response.send_message(
            "❌ 요일 입력이 올바르지 않습니다. "
            "'매일', '평일', '주말' 또는 "
            "'월수금' 형태로 입력해주세요.",
            ephemeral=True
        )

    if not validate_hours(
        start_hour,
        end_hour
    ):
        return await interaction.response.send_message(
            "❌ 시간 설정이 올바르지 않습니다.",
            ephemeral=True
        )

    try:

        with get_db() as conn:

            for day_of_week in target_days:

                for hour in range(
                    start_hour,
                    end_hour
                ):

                    row = conn.execute(
                        """
                        SELECT busy_mask
                        FROM recurring_schedule
                        WHERE day_of_week=? AND hour=?
                        """,
                        (
                            day_of_week,
                            hour
                        )
                    ).fetchone()

                    current_mask = (
                        row["busy_mask"]
                        if row
                        else 0
                    )

                    new_mask = (
                        current_mask
                        | user_mask
                    )

                    conn.execute(
                        """
                        INSERT INTO recurring_schedule
                            (day_of_week, hour, busy_mask)
                        VALUES (?, ?, ?)
                        ON CONFLICT(day_of_week, hour)
                        DO UPDATE SET
                            busy_mask=excluded.busy_mask
                        """,
                        (
                            day_of_week,
                            hour,
                            new_mask
                        )
                    )

            conn.commit()

    except sqlite3.Error:
        logger.exception(
            "반복 일정 등록 중 DB 오류"
        )

        return await interaction.response.send_message(
            "❌ DB 오류로 고정 일정 등록에 실패했습니다.",
            ephemeral=True
        )

    await interaction.response.send_message(
        f"✅ 매주 **{get_day_names(target_days)}** "
        f"{start_hour}시~{end_hour}시 "
        f"고정 불가 등록 완료"
    )


# ============================================================
# /고정취소
# ============================================================

@bot.tree.command(name="고정취소", description="등록했던 고정 불가 일정을 취소합니다.")
@app_commands.describe(
    days="입력 예: 매일, 평일, 주말, 월수금, 화,목",
    start_hour="시작 시간 (0~23)",
    end_hour="종료 시간 (1~24)"
)
async def cancel_recurring(
    interaction: discord.Interaction,
    days: str,
    start_hour: app_commands.Range[int, 0, 23],
    end_hour: app_commands.Range[int, 1, 24]
):

    user_mask = get_user_mask(
        interaction.user.id
    )

    if user_mask is None:
        return await interaction.response.send_message(
            "❌ 권한이 없습니다.",
            ephemeral=True
        )

    target_days = parse_days(days)

    if not target_days:
        return await interaction.response.send_message(
            "❌ 요일 입력이 올바르지 않습니다.",
            ephemeral=True
        )

    if not validate_hours(
        start_hour,
        end_hour
    ):
        return await interaction.response.send_message(
            "❌ 시간 설정이 올바르지 않습니다.",
            ephemeral=True
        )

    try:

        with get_db() as conn:

            for day_of_week in target_days:

                for hour in range(
                    start_hour,
                    end_hour
                ):

                    row = conn.execute(
                        """
                        SELECT busy_mask
                        FROM recurring_schedule
                        WHERE day_of_week=? AND hour=?
                        """,
                        (
                            day_of_week,
                            hour
                        )
                    ).fetchone()

                    if not row:
                        continue

                    current_mask = (
                        row["busy_mask"]
                    )

                    new_mask = (
                        current_mask
                        & ~user_mask
                    )

                    if new_mask == 0:

                        conn.execute(
                            """
                            DELETE FROM recurring_schedule
                            WHERE day_of_week=? AND hour=?
                            """,
                            (
                                day_of_week,
                                hour
                            )
                        )

                    else:

                        conn.execute(
                            """
                            UPDATE recurring_schedule
                            SET busy_mask=?
                            WHERE day_of_week=? AND hour=?
                            """,
                            (
                                new_mask,
                                day_of_week,
                                hour
                            )
                        )

                    # ------------------------------------------------
                    # 해당 반복 일정이 더 이상 본인에게 존재하지
                    # 않는다면, 해당 요일/시간의 예외도 정리합니다.
                    #
                    # 예:
                    # 월요일 20시 고정 불가
                    # ↓
                    # /고정취소 월 20 21
                    #
                    # 기존 특정 날짜의 free_mask 때문에
                    # 이상한 예외 데이터가 남지 않도록 처리.
                    # ------------------------------------------------

                    remaining_recurring_row = conn.execute(
                        """
                        SELECT busy_mask
                        FROM recurring_schedule
                        WHERE day_of_week=? AND hour=?
                        """,
                        (
                            day_of_week,
                            hour
                        )
                    ).fetchone()

                    remaining_recurring_mask = (
                        remaining_recurring_row["busy_mask"]
                        if remaining_recurring_row
                        else 0
                    )

                    if not (
                        remaining_recurring_mask
                        & user_mask
                    ):

                        specific_rows = conn.execute(
                            """
                            SELECT date, busy_mask, free_mask
                            FROM specific_schedule
                            WHERE hour=?
                            """,
                            (hour,)
                        ).fetchall()

                        for specific_row in specific_rows:

                            target_specific_date = (
                                datetime.strptime(
                                    specific_row["date"],
                                    "%Y-%m-%d"
                                ).date()
                            )

                            if (
                                target_specific_date.weekday()
                                != day_of_week
                            ):
                                continue

                            old_busy = specific_row[
                                "busy_mask"
                            ]

                            old_free = specific_row[
                                "free_mask"
                            ]

                            new_free = (
                                old_free
                                & ~user_mask
                            )

                            if (
                                old_busy == 0
                                and new_free == 0
                            ):
                                conn.execute(
                                    """
                                    DELETE FROM specific_schedule
                                    WHERE date=? AND hour=?
                                    """,
                                    (
                                        specific_row["date"],
                                        hour
                                    )
                                )

                            else:

                                conn.execute(
                                    """
                                    UPDATE specific_schedule
                                    SET free_mask=?
                                    WHERE date=? AND hour=?
                                    """,
                                    (
                                        new_free,
                                        specific_row["date"],
                                        hour
                                    )
                                )

            conn.commit()

    except sqlite3.Error:
        logger.exception(
            "반복 일정 취소 중 DB 오류"
        )

        return await interaction.response.send_message(
            "❌ DB 오류로 고정 일정 취소에 실패했습니다.",
            ephemeral=True
        )

    await interaction.response.send_message(
        f"✅ 매주 **{get_day_names(target_days)}** "
        f"{start_hour}시~{end_hour}시 "
        f"고정 일정이 취소되었습니다."
    )


# ============================================================
# /스케줄
# ============================================================

@bot.tree.command(name="상세스케줄", description="오늘부터 5일간의 상세 스케줄 표를 확인합니다.")
async def show_detailed_schedule(
    interaction: discord.Interaction
):

    today = today_kst()

    dates = [
        today + timedelta(days=i)
        for i in range(5)
    ]

    DATE_SPACES = [
        3, 1, 1, 1, 0
    ]

    DAY_SPACES = [
        5, 2, 2, 2, 2
    ]

    EMOJI_SPACES = [
        1, 1, 1, 1, 1
    ]

    if len(DATE_SPACES) != 5:
        raise ValueError(
            "DATE_SPACES는 반드시 5개여야 합니다."
        )

    if len(DAY_SPACES) != 5:
        raise ValueError(
            "DAY_SPACES는 반드시 5개여야 합니다."
        )

    if len(EMOJI_SPACES) != 5:
        raise ValueError(
            "EMOJI_SPACES는 반드시 5개여야 합니다."
        )

    # ========================================================
    # DB에서 전체 기간을 한 번에 가져오기
    # ========================================================

    with get_db() as conn:

        specific_map = load_specific_schedule(
            conn,
            dates
        )

        recurring_map = load_recurring_schedule(
            conn
        )

    # ========================================================
    # 헤더
    # ========================================================

    day_names = [
        "월",
        "화",
        "수",
        "목",
        "금",
        "토",
        "일"
    ]

    headers_list = []

    for i, d in enumerate(dates):

        date_text = (
            f"{d.month:02d}/{d.day:02d}"
        )

        date_text = (
            " " * DATE_SPACES[i]
            + date_text
        )

        headers_list.append(
            date_text
        )

    headers = "   ".join(
        headers_list
    )

    weekdays_list = []

    for i, d in enumerate(dates):

        day_text = day_names[
            d.weekday()
        ]

        day_text = (
            " " * DAY_SPACES[i]
            + day_text
        )

        weekdays_list.append(
            day_text
        )

    weekdays = "     ".join(
        weekdays_list
    )

    # ========================================================
    # Embed 1
    # ========================================================

    embed1 = discord.Embed(
        title="📅 Group's availability - Next 5 Days (자세히)",
        color=0x2b2d31
    )

    grid1 = (
        "```text\n"
        + (" " * 2)
        + headers
        + "\n"
        + (" " * 2)
        + weekdays
        + "\n"
    )

    # ========================================================
    # Embed 2
    # ========================================================

    embed2 = discord.Embed(
        color=0x2b2d31
    )

    grid2 = ""

    # ========================================================
    # 시간표
    # ========================================================

    for hour in range(24):

        time_label = (
            f"{hour:02d} "
            if hour % 3 == 0
            else "   "
        )

        row_emojis = []

        for i, d in enumerate(dates):

            final_mask = get_final_mask(
                d,
                hour,
                specific_map,
                recurring_map
            )

            day_emojis = (
                (" " * EMOJI_SPACES[i])
                + get_individual_emojis(
                    final_mask
                )
            )

            row_emojis.append(
                day_emojis
            )

        row_str = (
            f"{time_label}"
            f"{' '.join(row_emojis)}\n"
        )

        if hour < 12:
            grid1 += row_str
        else:
            grid2 += row_str

    # ========================================================
    # 코드블록
    # ========================================================

    grid1 += "```"

    embed1.description = grid1

    grid2 = (
        "```text\n"
        + grid2
        + "```"
    )

    embed2.description = grid2

    embed2.set_footer(
        text=(
                    "🟥:개리길이 불가 | "
                    "🟦:소벌도리 불가 | "
                    "🟨:주말을월일로 불가 | "
                    "⬜:가능\n"
                )
    )

    await interaction.response.send_message(
        embed=embed1
    )

    await interaction.followup.send(
        embed=embed2
    )


# ============================================================
# /간단스케줄
# ============================================================

@bot.tree.command(name="스케줄", description="오늘부터 7일간의 스케줄 표를 확인합니다.")
async def show_simple_schedule(
    interaction: discord.Interaction
):

    today = today_kst()

    dates = [
        today + timedelta(days=i)
        for i in range(7)
    ]

    day_names = [
        "월",
        "화",
        "수",
        "목",
        "금",
        "토",
        "일"
    ]

    # ========================================================
    # Embed
    # ========================================================

    embed = discord.Embed(
        title=(
            "📅 Group's availability - "
            f"Week of {today.strftime('%b %d')} (간단히)"
        ),
        color=0x2b2d31
    )

    DATE_SPACES = [
        0, 1, 1, 1, 1, 1, 1
    ]

    DAY_SPACES = [
        2, 4, 4, 5, 4, 5, 4
    ]

    EMOJI_SPACES = [
        2, 3, 4, 4, 4, 4, 3
    ]

    TIME_WIDTH = 2

    if len(DATE_SPACES) != 7:
        raise ValueError(
            "DATE_SPACES는 반드시 7개여야 합니다."
        )

    if len(DAY_SPACES) != 7:
        raise ValueError(
            "DAY_SPACES는 반드시 7개여야 합니다."
        )

    if len(EMOJI_SPACES) != 7:
        raise ValueError(
            "EMOJI_SPACES는 반드시 7개여야 합니다."
        )

    # ========================================================
    # DB에서 한 번만 조회
    # ========================================================

    with get_db() as conn:

        specific_map = load_specific_schedule(
            conn,
            dates
        )

        recurring_map = load_recurring_schedule(
            conn
        )

    # ========================================================
    # 표
    # ========================================================

    grid = "```text\n"

    # ========================================================
    # 날짜
    # ========================================================

    grid += " " * TIME_WIDTH

    for i, d in enumerate(dates):

        date_text = (
            f"{d.month:02d}/{d.day:02d}"
        )

        grid += (
            " " * DATE_SPACES[i]
        )

        grid += date_text

    grid += "\n"

    # ========================================================
    # 요일
    # ========================================================

    grid += " " * TIME_WIDTH

    for i, d in enumerate(dates):

        day_text = day_names[
            d.weekday()
        ]

        grid += (
            " " * DAY_SPACES[i]
        )

        grid += day_text

    grid += "\n"

    # ========================================================
    # 시간별 일정
    # ========================================================

    for hour in range(24):

        if hour % 3 == 0:
            time_label = f"{hour:02d}"
        else:
            time_label = "  "

        grid += time_label

        for i, d in enumerate(dates):

            final_mask = get_final_mask(
                d,
                hour,
                specific_map,
                recurring_map
            )

            emoji = get_emoji(
                final_mask
            )

            grid += (
                " " * EMOJI_SPACES[i]
            )

            grid += emoji

        grid += "\n"

    grid += "```"

    embed.description = grid

    embed.set_footer(
        text=(
            "불가능한 인원) 🟥:개리길이 🟦:소벌도리 🟨:주말을월일로\n"
            "🟪:개+소 🟧:개+주 🟩:소+주 ⬛:전원불가 ⬜:전원가능"
        )
    )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# /조회
# ============================================================

@bot.tree.command(name="날짜조회", description="특정 날짜의 상세 불가 일정을 요약해서 보여줍니다.")
@app_commands.describe(
    date="날짜 (예: 10/20)"
)
async def check_schedule(
    interaction: discord.Interaction,
    date: str
):

    try:
        parsed_datetime = parse_user_date(
            date
        )

    except ValueError:

        return await interaction.response.send_message(
            "❌ 날짜 형식이 올바르지 않습니다. "
            "'10/20' 형태로 입력해주세요.",
            ephemeral=True
        )

    target_date = parsed_datetime.date()

    db_date_str = target_date.strftime(
        "%Y-%m-%d"
    )

    day_of_week = target_date.weekday()

    # ========================================================
    # DB 2번이 아니라 사실상 하루 전체를 한 번에 로드
    # ========================================================

    with get_db() as conn:

        specific_rows = conn.execute(
            """
            SELECT hour, busy_mask, free_mask
            FROM specific_schedule
            WHERE date=?
            """,
            (
                db_date_str,
            )
        ).fetchall()

        recurring_rows = conn.execute(
            """
            SELECT hour, busy_mask
            FROM recurring_schedule
            WHERE day_of_week=?
            """,
            (
                day_of_week,
            )
        ).fetchall()

    specific_map = {
        (
            db_date_str,
            row["hour"]
        ): (
            row["busy_mask"],
            row["free_mask"]
        )
        for row in specific_rows
    }

    recurring_map = {
        (
            day_of_week,
            row["hour"]
        ): row["busy_mask"]
        for row in recurring_rows
    }

    # ========================================================
    # 연속 시간 묶기
    # ========================================================

    schedule_blocks = []

    user_masks = [1, 2, 4]

    for user_mask in user_masks:

        current_active = False
        start_hour = None

        for hour in range(25):

            if hour < 24:

                final_mask = get_final_mask(
                    target_date,
                    hour,
                    specific_map,
                    recurring_map
                )

                is_unavailable = bool(
                    final_mask & user_mask
                )

            else:
                # 24시는 마지막 구간을 닫기 위한 가상 시간
                is_unavailable = False

            # -----------------------------------------------
            # 불가능 시작
            # -----------------------------------------------

            if is_unavailable and not current_active:

                current_active = True
                start_hour = hour

            # -----------------------------------------------
            # 불가능 종료
            # -----------------------------------------------

            elif not is_unavailable and current_active:

                schedule_blocks.append(
                    (
                        start_hour,
                        hour,
                        user_mask
                    )
                )

                current_active = False
                start_hour = None

    # ========================================================
    # Embed
    # ========================================================

    day_names = [
        "월",
        "화",
        "수",
        "목",
        "금",
        "토",
        "일"
    ]

    day_name = day_names[
        day_of_week
    ]

    embed = discord.Embed(
        title=(
            f"📅 {target_date.month}/"
            f"{target_date.day} "
            f"({day_name}) 불가 일정 요약"
        ),
        color=0x2b2d31
    )

    if not schedule_blocks:

        embed.description = (
            "✅ 등록된 불가 일정이 없습니다. "
            "전원 가능합니다! (⬜)"
        )

    else:

        lines = []

        user_names = {
            1: "개리길이",
            2: "소벌도리",
            4: "주말을월일로",
        }

        user_emojis = {
            1: "🟥",
            2: "🟦",
            4: "🟨",
        }

        for start, end, mask in schedule_blocks:

            end_display = (
                f"{end:02d}:00"
                if end < 24
                else "24:00"
            )

            lines.append(
                f"• `{start:02d}:00 ~ "
                f"{end_display}` : "
                f"**{user_names[mask]}** "
                f"{user_emojis[mask]}"
            )

        embed.description = "\n".join(
            lines
        )

        embed.set_footer(
            text=(
                "표시된 시간을 제외한 "
                "나머지 시간은 전원 가능합니다."
            )
        )

    await interaction.response.send_message(
        embed=embed
    )


# ============================================================
# /고정목록
# ============================================================

@bot.tree.command(
    name="고정목록",
    description="현재 등록되어 있는 모든 고정 일정을 확인합니다."
)
async def show_recurring_schedule(
    interaction: discord.Interaction
):

    # --------------------------------------------------------
    # DB에서 고정 일정 전체 조회
    # --------------------------------------------------------

    try:
        with get_db() as conn:

            rows = conn.execute(
                """
                SELECT day_of_week, hour, busy_mask
                FROM recurring_schedule
                ORDER BY day_of_week ASC, hour ASC
                """
            ).fetchall()

    except sqlite3.Error:
        logger.exception(
            "고정 일정 목록 조회 중 DB 오류"
        )

        return await interaction.response.send_message(
            "❌ DB 오류로 고정 일정 목록을 불러오지 못했습니다.",
            ephemeral=True
        )

    # --------------------------------------------------------
    # 등록된 고정 일정이 없는 경우
    # --------------------------------------------------------

    if not rows:

        embed = discord.Embed(
            title="📋 고정 일정 목록",
            description="현재 등록된 고정 일정이 없습니다.",
            color=0x2b2d31
        )

        await interaction.response.send_message(
            embed=embed
        )

        return

    # --------------------------------------------------------
    # (요일, 시간) -> mask 형태로 변환
    # --------------------------------------------------------

    schedule_map = {
        (
            row["day_of_week"],
            row["hour"]
        ): row["busy_mask"]
        for row in rows
    }

    # --------------------------------------------------------
    # 요일 이름
    # --------------------------------------------------------

    day_names = [
        "월",
        "화",
        "수",
        "목",
        "금",
        "토",
        "일"
    ]

    # --------------------------------------------------------
    # 사람별 고정 일정 생성
    #
    # 핵심:
    # 같은 시간에 여러 명이 겹쳐 있어도 합치지 않습니다.
    # 각 사람의 일정은 완전히 독립적으로 처리합니다.
    # --------------------------------------------------------

    schedules_by_user = {}

    for info in USER_INFO.values():

        user_mask = info["mask"]
        user_name = info["name"]
        user_emoji = info["emoji"]

        user_blocks = []

        # 월요일 ~ 일요일
        for day_of_week in range(7):

            current_busy = False
            start_hour = None

            # 24시는 마지막 구간을 닫기 위한 가상 시간
            for hour in range(25):

                if hour < 24:

                    current_hour_mask = schedule_map.get(
                        (day_of_week, hour),
                        0
                    )

                    is_busy = bool(
                        current_hour_mask & user_mask
                    )

                else:
                    is_busy = False

                # --------------------------------------------
                # 불가 일정 시작
                # --------------------------------------------

                if is_busy and not current_busy:

                    start_hour = hour
                    current_busy = True

                # --------------------------------------------
                # 불가 일정 종료
                # --------------------------------------------

                elif not is_busy and current_busy:

                    user_blocks.append(
                        (
                            day_of_week,
                            start_hour,
                            hour
                        )
                    )

                    start_hour = None
                    current_busy = False

        schedules_by_user[user_mask] = {
            "name": user_name,
            "emoji": user_emoji,
            "blocks": user_blocks
        }

    # --------------------------------------------------------
    # Embed
    # --------------------------------------------------------

    embed = discord.Embed(
        title="📋 매주 반복 고정 일정",
        description="인원별로 등록된 고정 일정을 정리했습니다.",
        color=0x2b2d31
    )

    # --------------------------------------------------------
    # 사람별 출력
    # --------------------------------------------------------

    for user_mask, user_data in schedules_by_user.items():

        user_name = user_data["name"]
        user_emoji = user_data["emoji"]
        blocks = user_data["blocks"]

        if not blocks:

            value = "등록된 고정 일정이 없습니다."

        else:

            lines = []

            for day_of_week, start_hour, end_hour in blocks:

                end_display = (
                    f"{end_hour:02d}:00"
                    if end_hour < 24
                    else "24:00"
                )

                lines.append(
                    f"`{day_names[day_of_week]} "
                    f"{start_hour:02d}:00 ~ {end_display}`"
                )

            value = "\n".join(lines)

        embed.add_field(
            name=f"{user_emoji} {user_name}",
            value=value,
            inline=False
        )

    # --------------------------------------------------------
    # Footer
    # --------------------------------------------------------

    embed.set_footer(
        text=(
            "각 인원의 고정 일정은 서로 독립적으로 표시됩니다."
        )
    )

    await interaction.response.send_message(
        embed=embed
    )



# ============================================================
# /명령어 도우미
# ============================================================

@bot.tree.command(
    name="명령어",
    description="현재 사용 가능한 모든 명령어를 확인합니다."
)
async def show_commands(
    interaction: discord.Interaction
):

    embed = discord.Embed(
        title="📖 일정 봇 명령어 안내",
        description="현재 사용할 수 있는 명령어 목록입니다.",
        color=0x2b2d31
    )

    embed.add_field(
        name="📅 일정 등록",
        value=(
            "============================\n"
            "/일정 `날짜` `시작시간` `종료시간`\n"
            "특정 날짜의 불가능한 시간을 등록합니다.\n"
            "예: `/일정 10/19 9 18`\n\n"

            "/일 `날짜` `시작시간` `종료시간`\n"
            "위 `/일정`의 단축 명령어입니다.\n"
            "============================"
        ),
        inline=False
    )

    embed.add_field(
        name="❌ 일정 취소",
        value=(
            "============================\n"
            "/일정취소 `날짜` `시작시간` `종료시간`\n"
            "등록한 특정 날짜의 불가 일정을 취소합니다.\n"
            "예: `/일정취소 10/19 9 18`\n\n"

            "/일취 `날짜` `시작시간` `종료시간`\n"
            "위 `/일정취소`의 단축 명령어입니다.\n"
            "============================"
        ),
        inline=False
    )

    embed.add_field(
        name="🔒 고정 일정",
        value=(
            "============================\n"
            "/고정 `요일` `시작시간` `종료시간`\n"
            "매주 반복되는 불가능한 시간을 등록합니다.\n"
            "요일 입력: `매일`, `평일`, `주말`, `월수금`, `화,목`\n"
            "예: `/고정 평일 9 16`\n\n"

            "/고 `요일` `시작시간` `종료시간`\n"
            "위 `/고정`의 단축 명령어입니다.\n"
            "============================"
        ),
        inline=False
    )

    embed.add_field(
        name="🔓 고정 일정 취소",
        value=(
            "============================\n"
            "/고정취소 `요일` `시작시간` `종료시간`\n"
            "등록한 매주 반복 일정을 취소합니다.\n"
            "예: `/고정취소 월수금 9 16`\n\n"

            "/고취 `요일` `시작시간` `종료시간`\n"
            "위 `/고정취소`의 단축 명령어입니다.\n"
            "============================"
        ),
        inline=False
    )

    embed.add_field(
        name="📊 스케줄 확인",
        value=(
            "============================\n"
            "/상세스케줄\n"
            "오늘부터 5일간의 상세 스케줄을 확인합니다.\n"
            "각 날짜마다 인원들의 가능 여부 상세하게 표시됩니다.\n\n"

            "/상스\n"
            "위 `/스케줄`의 단축 명령어입니다.\n"
            "============================"
        ),
        inline=False
    )

    embed.add_field(
        name="📋 스케줄",
        value=(
            "============================\n"
            "/스케줄\n"
            "오늘부터 7일간의 스케줄을 한눈에 확인합니다.\n"
            "한 칸에 여러 사람의 불가 상태가 색상으로 표시됩니다.\n\n"

            "/스\n"
            "위 `/스케줄`의 단축 명령어입니다.\n"
            "============================"
        ),
        inline=False
    )

    embed.add_field(
        name="🔎 특정 날짜 조회",
        value=(
            "============================\n"
            "/날짜조회 `날짜`\n"
            "특정 날짜의 불가능한 시간을 연속된 시간대로 묶어 보여줍니다.\n"
            "예: `/조회 8/25`\n\n"

            "/조 `날짜`\n"
            "위 `/조회`의 단축 명령어입니다.\n"
            "============================"
        ),
        inline=False
    )

    embed.add_field(
        name="📋 고정 일정 목록",
        value=(
            "============================\n"
            "/고정목록\n"
            "현재 등록되어 있는 매주 반복 고정 일정을 "
            "요일별로 확인합니다.\n\n"

            "/고목\n"
            "위 `/고정목록`의 단축 명령어입니다.\n"
            "============================"
        ),
        inline=False
    )

    embed.add_field(
        name="❔ 명령어 안내",
        value=(
            "============================\n"
            "/명령어\n"
            "현재 사용 가능한 명령어와 사용법을 확인합니다.\n"
            "============================"
        ),
        inline=False
    )

    embed.add_field(
        name="🎨 색상 안내",
        value=(
            "============================\n"
            "🟥 개리길이 불가\n"
            "🟦 소벌도리 불가\n"
            "🟨 주말을월일로 불가\n"
            "🟪 개+소 불가\n"
            "🟧 개+주 불가\n"
            "🟩 소+주 불가\n"
            "⬛ 개소주 불가\n"
            "⬜ 개소주 가능\n"
            "============================"
        ),
        inline=False
    )

    embed.set_footer(
        text="시간은 0~24시 기준이며, 날짜는 M/D 형식으로 입력합니다."
    )

    await interaction.response.send_message(
        embed=embed
    )



# ============================================================
# 슬래시 명령어 단축어 등록
#
# 같은 callback을 여러 Slash Command에 연결합니다.
# @bot.tree.command decorator를 중첩해서 사용하면
# "command function must be a coroutine function" 오류가
# 발생할 수 있으므로 여기에서 별도 Command로 등록합니다.
# ============================================================

def register_alias(
    alias_name: str,
    description: str,
    command: app_commands.Command
):
    alias_command = app_commands.Command(
        name=alias_name,
        description=description,
        callback=command.callback
    )

    bot.tree.add_command(alias_command)


register_alias(
    "일",
    "일정 단축 명령어",
    add_busy
)

register_alias(
    "일취",
    "일정취소 단축 명령어",
    cancel_busy
)

register_alias(
    "고",
    "고정 일정 단축 명령어",
    add_recurring
)

register_alias(
    "고취",
    "고정취소 단축 명령어",
    cancel_recurring
)

register_alias(
    "상스",
    "상세스케줄 단축 명령어",
    show_detailed_schedule
)

register_alias(
    "스",
    "스케줄 단축 명령어",
    show_simple_schedule
)

register_alias(
    "날",
    "날짜조회 단축 명령어",
    check_schedule
)

register_alias(
    "고목",
    "고정 일정 목록 단축 명령어",
    show_recurring_schedule
)


# ============================================================
# 슬래시 명령어 에러 처리
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):

    logger.error(
        "Slash command error: %s",
        error
    )

    traceback.print_exception(
        type(error),
        error,
        error.__traceback__
    )

    # 이미 응답했다면 followup
    if interaction.response.is_done():

        try:
            await interaction.followup.send(
                "❌ 명령어 처리 중 오류가 발생했습니다. "
                "콘솔 로그를 확인해주세요.",
                ephemeral=True
            )

        except discord.DiscordException:
            logger.exception(
                "에러 메시지 followup 전송 실패"
            )

    else:

        try:
            await interaction.response.send_message(
                "❌ 명령어 처리 중 오류가 발생했습니다. "
                "콘솔 로그를 확인해주세요.",
                ephemeral=True
            )

        except discord.DiscordException:
            logger.exception(
                "에러 메시지 전송 실패"
            )


# ============================================================
# 봇 실행
# ============================================================

if __name__ == "__main__":

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN이 설정되지 않았습니다."
        )

    bot.run(BOT_TOKEN)