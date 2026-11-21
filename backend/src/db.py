import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase,sessionmaker
_url=os.environ['DATABASE_URL']
_options={'pool_pre_ping':True}
# MySQL 是部署形态；测试用的 SQLite 不接受 READ COMMITTED 隔离级别参数。
if _url.startswith('mysql'):_options['isolation_level']='READ COMMITTED'
engine=create_engine(_url,**_options)
Session=sessionmaker(engine,expire_on_commit=False)
class Base(DeclarativeBase):pass
def session():
    with Session() as s:yield s
