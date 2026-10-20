#!/usr/bin/env python3
"""Prompt without echo; keep the API key only in this process's environment."""
import getpass
import os
import sys
from pathlib import Path

if __name__ == '__main__':
    os.chdir(Path(__file__).resolve().parent)
    if not os.environ.get('TYPESAFE_API_KEY'):
        key=getpass.getpass('TypeSafe API key (hidden): ').strip()
        if not key:
            sys.exit('An API key is required. Nothing was saved.')
        os.environ['TYPESAFE_API_KEY']=key
    import uvicorn
    uvicorn.run('app.main:app',host='127.0.0.1',port=int(os.environ.get('PORT','8765')),log_level='warning',access_log=False)
