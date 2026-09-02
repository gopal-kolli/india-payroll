#!/bin/bash

set -e

cd ~ || exit

sudo apt update
sudo apt remove mysql-server mysql-client
sudo apt install libcups2-dev redis-server mariadb-client libmariadb-dev

pip install frappe-bench

INDIA_PAYROLL_BRANCH=${BRANCH_TO_CLONE:-develop}

if [[ "$INDIA_PAYROLL_BRANCH" == "version-1" || "$INDIA_PAYROLL_BRANCH" == "version-1-hotfix" ]]; then
    FRAPPE_BRANCH="version-15"
    ERPNEXT_BRANCH="version-15"
    HRMS_BRANCH="version-15"
elif [[ "$INDIA_PAYROLL_BRANCH" == version-16* || "$INDIA_PAYROLL_BRANCH" == codex/epf-wage-fix-v16 ]]; then
    FRAPPE_BRANCH="version-16"
    ERPNEXT_BRANCH="version-16"
    HRMS_BRANCH="version-16"
else
    FRAPPE_BRANCH="develop"
    ERPNEXT_BRANCH="develop"
    HRMS_BRANCH="develop"
fi

echo "Using Frappe branch:      $FRAPPE_BRANCH"
echo "Using ERPNext branch:     $ERPNEXT_BRANCH"
echo "Using HRMS branch:        $HRMS_BRANCH"
echo "Using India Payroll branch: $INDIA_PAYROLL_BRANCH"

git clone https://github.com/frappe/frappe --branch "$FRAPPE_BRANCH" --depth 1
bench init --skip-assets --frappe-path ~/frappe --python "$(which python)" frappe-bench

mkdir ~/frappe-bench/sites/test_site
cp -r "${GITHUB_WORKSPACE}/.github/helper/site_config.json" ~/frappe-bench/sites/test_site/

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "SET GLOBAL character_set_server = 'utf8mb4'"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "SET GLOBAL collation_server = 'utf8mb4_unicode_ci'"

mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "CREATE USER 'test_frappe'@'localhost' IDENTIFIED BY 'test_frappe'"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "CREATE DATABASE test_frappe"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "GRANT ALL PRIVILEGES ON \`test_frappe\`.* TO 'test_frappe'@'localhost'"
mariadb --host 127.0.0.1 --port 3306 -u root -proot -e "FLUSH PRIVILEGES"

install_wkhtmltopdf() {
    wget -O /tmp/wkhtmltox.tar.xz https://github.com/frappe/wkhtmltopdf/raw/master/wkhtmltox-0.12.3_linux-generic-amd64.tar.xz
    tar -xf /tmp/wkhtmltox.tar.xz -C /tmp
    sudo mv /tmp/wkhtmltox/bin/wkhtmltopdf /usr/local/bin/wkhtmltopdf
    sudo chmod o+x /usr/local/bin/wkhtmltopdf
}
install_wkhtmltopdf &

cd ~/frappe-bench || exit

sed -i 's/watch:/# watch:/g' Procfile
sed -i 's/schedule:/# schedule:/g' Procfile
sed -i 's/socketio:/# socketio:/g' Procfile
sed -i 's/redis_socketio:/# redis_socketio:/g' Procfile

bench get-app payments
bench get-app https://github.com/frappe/erpnext --branch "$ERPNEXT_BRANCH" --resolve-deps
bench get-app https://github.com/frappe/hrms --branch "$HRMS_BRANCH"
bench get-app india_payroll "${GITHUB_WORKSPACE}"

bench setup requirements --dev

bench start &>> ~/frappe-bench/bench_start.log &
CI=Yes bench build --app frappe &
bench --site test_site reinstall --yes

bench --verbose --site test_site install-app hrms
bench --verbose --site test_site install-app india_payroll
