#!/bin/bash
# PowerStat-Analytics 一键部署脚本
set -e

echo "=========================================="
echo "PowerStat-Analytics 安装部署"
echo "=========================================="

# 检查 Python 版本
python3 --version 2>/dev/null || { echo "错误: 需要 Python 3.9+"; exit 1; }

# 创建虚拟环境
if [ ! -d "venv" ]; then
    echo "[1/4] 创建虚拟环境..."
    python3 -m venv venv
fi

echo "[2/4] 激活虚拟环境..."
source venv/bin/activate

echo "[3/4] 安装依赖..."
pip install --upgrade pip
pip install -r requirements.txt

# 创建 .env 文件（如果不存在）
if [ ! -f ".env" ]; then
    echo "[4/4] 创建 .env 配置文件..."
    cp .env.example .env
    echo ""
    echo "=========================================="
    echo "请编辑 .env 文件，填入邮箱账号和授权码："
    echo "  EMAIL_ACCOUNT=your_email@qq.com"
    echo "  EMAIL_AUTH_CODE=your_imap_auth_code"
    echo "=========================================="
else
    echo "[4/4] .env 已存在，跳过"
fi

# 创建必要目录
mkdir -p output/{data,charts,archive,temp_attachments} logs

echo ""
echo "安装完成！使用方式："
echo "  source venv/bin/activate"
echo "  python main.py fetch    # 抓取邮件并处理"
echo "  python main.py local    # 处理本地附件"
echo "  python main.py query    # 查询数据"
echo "  python main.py viz      # 生成图表"
echo "  python main.py export   # 导出 CSV"
echo "  python main.py info     # 查看统计"
