from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, echo=settings.debug)

async_session_maker = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def ensure_status_enum_values() -> list[str]:
    """把后加的发票状态补进 PostgreSQL 的 invoicestatus 类型。

    ``Base.metadata.create_all`` 只在**建表**时创建 enum 类型：表已经存在时
    它一句 DDL 都不会发，于是往 status 写一个新状态直接报
    ``invalid input value for enum invoicestatus``。所以新增状态值必须在这里
    补齐 —— 交付包是双击启动的，不该要求用户手工跑迁移 SQL。

    写入的是枚举的**成员名**（``REJECTED``）而不是 Python 侧的取值
    （``已拒绝``）：``Enum(InvoiceStatus)`` 默认用 ``.name`` 落库，库里现有
    的标签就是 ``UPLOADED, PROCESSING, ...`` 这一串英文名。

    返回本次新增的标签，供启动日志使用。重复执行是安全的。
    """
    # 延迟导入：models 反向依赖本模块的 Base，顶层 import 会成环
    from app.models.invoice import InvoiceStatus

    added: list[str] = []
    async with engine.begin() as conn:
        result = await conn.execute(text(
            "SELECT e.enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'invoicestatus'"
        ))
        existing = {row[0] for row in result}
        if not existing:
            # 类型还不存在（全新库）：create_all 已经按最新的枚举建好了
            return added

        for member in InvoiceStatus:
            if member.name in existing:
                continue
            # ADD VALUE ... IF NOT EXISTS 需要 PG 12+；内嵌的 pgserver 是 16，
            # 老版本上语法不认，这里不该把整个后端起不来。
            try:
                await conn.execute(text(
                    f"ALTER TYPE invoicestatus ADD VALUE IF NOT EXISTS '{member.name}'"
                ))
                added.append(member.name)
            except Exception as exc:  # noqa: BLE001 - 迁移失败要能看见但不致命
                import logging
                logging.getLogger(__name__).warning(
                    "无法为 invoicestatus 补加 %s：%s", member.name, exc
                )
    return added
