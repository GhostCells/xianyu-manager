"""Deployment-only ownership handoff for registered account product directories.

No recursive chmod/chown, database updates, asset edits, network or application
startup. Web/runtime processes never invoke this tool or receive sudo rights.
"""
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from xianyu_manager.product_ownership import has_foreign_product_use


def plan_directories(database, library, account_id, uid):
    if library.is_symlink() or not library.is_dir() or library.resolve() != library:
        raise ValueError('Library must be a canonical real directory')
    c=sqlite3.connect('file:'+str(database)+'?mode=ro',uri=True)
    rows=c.execute('''SELECT p.dir_name FROM products p JOIN account_products ap ON ap.product_dir_name=p.dir_name
        WHERE ap.account_id=? AND p.catalog_status NOT IN ('legacy','listing_only')
        ORDER BY p.dir_name''',(account_id,)).fetchall()
    if c.execute("SELECT 1 FROM orders WHERE account_id=? AND delivery_status='sending'",(account_id,)).fetchone():
        raise ValueError('SEND_IN_PROGRESS')
    result=[]
    for (name,) in rows:
        if has_foreign_product_use(c, name, account_id):continue
        # Old listing placeholders are database records, not product folders.
        if re.fullmatch(r'__listing__\d+',name):continue
        if not re.fullmatch(r'\d{2}-[^/\\:]+',name):
            raise ValueError('Invalid registered product directory')
        p=library/name
        if not p.exists() and not p.is_symlink():continue
        s=p.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_dev!=library.stat().st_dev:
            raise ValueError('Non-directory, symlink or mounted product: '+name)
        if s.st_uid not in {0,uid}:
            raise ValueError('Unexpected owner: '+name)
        mode=stat.S_IMODE(s.st_mode)
        if mode & 0o7000:raise ValueError('Unexpected special permission bits: '+name)
        if s.st_uid!=uid or mode & 0o700!=0o700:
            result.append({'name':name,'inode':s.st_ino,'device':s.st_dev,'uid':s.st_uid,'gid':s.st_gid,'mode':mode,'new_uid':uid,'new_mode':mode|0o700})
    c.close()
    return result


def apply_directories(library, plan):
    for entry in plan:
        fd=os.open(library/entry['name'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            s=os.fstat(fd)
            if (s.st_ino,s.st_dev,s.st_uid,s.st_gid,stat.S_IMODE(s.st_mode)) != (entry['inode'],entry['device'],entry['uid'],entry['gid'],entry['mode']):
                raise ValueError('Directory changed since inspection')
            if s.st_uid!=entry['new_uid']:os.fchown(fd,entry['new_uid'],-1)
            os.fchmod(fd,entry['new_mode'])
        finally:os.close(fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',required=True,type=Path)
    parser.add_argument('--library',required=True,type=Path)
    parser.add_argument('--account-id',required=True,type=int)
    parser.add_argument('--runtime-user',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--journal',type=Path)
    args=parser.parse_args()
    uid=pwd.getpwnam(args.runtime_user).pw_uid
    if uid==0:raise ValueError('Runtime user cannot be root')
    plan=plan_directories(args.database,args.library,args.account_id,uid)
    if args.apply:
        if os.geteuid()!=0 or args.journal is None:raise ValueError('Administrator and permission journal required')
        fd=os.open(args.journal,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:json.dump({'library':str(args.library),'account_id':args.account_id,'plan':plan},f,ensure_ascii=False)
        apply_directories(args.library,plan)
        assert not plan_directories(args.database,args.library,args.account_id,uid)
    print(json.dumps({'applied':args.apply,'directory_count':len(plan),'directories':[p['name'] for p in plan],'recursive':False,'database_updated':False},ensure_ascii=False))


if __name__=='__main__':main()
