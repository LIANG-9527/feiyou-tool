#!/bin/bash
# 发版打包：把"该传的代码"整备到 dist/ 一个目录，上传只传 dist/，不用挑文件
cd "$(dirname "$0")/.." || exit 1
rm -rf dist
rsync -a --exclude dist --exclude var --exclude __pycache__ --exclude '*.pyc' \
      --exclude .DS_Store --exclude .zcode --exclude .git --exclude .env --exclude graphify-out ./ dist/
echo "dist/ 就绪: $(du -sh dist | cut -f1)，条目 $(find dist -type f | wc -l | tr -d ' ')"
# 注意：.env（模型密钥）不进包——服务器需单独放置 .env（GEO_LLM_API_KEY=...），否则模型调用会失败
