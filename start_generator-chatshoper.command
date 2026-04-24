#!/bin/bash
export PYTHONUTF8=1

cd "$HOME/Desktop/generator-chatshoper-final" || exit 1

if [ -d ".venv" ]; then
  source .venv/bin/activate
elif [ -d "venv" ]; then
  source venv/bin/activate
fi

if [ -x ".venv/bin/python" ]; then
  .venv/bin/python -m streamlit run app.py
else
  python3 -m streamlit run app.py
fi
