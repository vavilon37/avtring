#!/bin/bash
pip install -r requirements.txt
python -m playwright install chromium --with-deps
python main.py
