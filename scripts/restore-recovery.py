#!/usr/bin/env python3
"""Restore a verified recovery set into an EMPTY data directory and isolated/fresh Qdrant."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.database import Database
from app.services.recovery import RecoveryService
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('name');p.add_argument('--confirm',required=True);p.add_argument('--target',required=True,type=Path)
p.add_argument('--qdrant-url',required=True);p.add_argument('--prefix',default='')
a=p.parse_args()
if a.confirm!=a.name:p.error('确认名称必须与恢复包一致')
print(json.dumps(RecoveryService(Database(settings.database_path),settings).restore_to(a.name,a.target,a.qdrant_url,a.prefix),ensure_ascii=False))
