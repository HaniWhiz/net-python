# پاک‌سازی روزانه‌ی ARP در سوییچ‌های Nexus 3000

این پروژه جدول ARP سوییچ‌های Cisco Nexus 3000 (NX-OS) را روزی یک بار پاک می‌کند و نتیجه را ثبت می‌کند.

سه روش برای این کار در این مخزن هست، از ساده‌ترین تا کامل‌ترین. هر کدام را که مناسب‌تر است انتخاب کنید.

## اهداف

- پاک کردن ورودی‌های ARP روی همه‌ی سوییچ‌های Nexus 3000، روزی یک بار.
- شمردن ورودی‌های ARP قبل و بعد از پاک کردن، تا معلوم باشد کار انجام شده.
- اگر یک سوییچ خطا داد، کار روی بقیه ادامه پیدا کند و علت خطا ثبت شود.
- فقط دو دستور فرستاده شود: شمارش ARP و پاک کردن ARP. پیکربندی سوییچ هرگز تغییر نکند.
- رمزها نه در کد باشند، نه در لاگ‌ها و نه در این مخزن.

## انتخاب روش

| | ۱. اسکریپت ساده | ۲. اسکریپت کامل | ۳. روی خود سوییچ |
| --- | --- | --- | --- |
| فایل | `arp_clear_v2.py` | `clear_arp.py` + `devices.csv` | `nxos_scheduler_arp_clear.txt` |
| محل اجرا | کامپیوتر خودتان | سرور (Windows Task Scheduler) | هر سوییچ |
| پایتون لازم است؟ | بله | بله | خیر |
| فهرست سوییچ‌ها | داخل اسکریپت | `devices.csv` | روی هر سوییچ جداگانه |
| نتیجه | صفحه + فایل لاگ | لاگ + CSV، ایمیل اختیاری | فایل متنی روی سوییچ |
| مناسب برای | اجرای دستی، یادگیری | اجرای خودکار روزانه | وقتی سروری در کار نیست |

## قبل از شروع (برای هر سه روش)

روی یک سوییچ، دستورها را دستی امتحان کنید:

```text
clear ip arp ?
clear ip arp vrf ?
show ip arp summary
```

- دستور پاک کردن را دقیقاً به همان شکلی بنویسید که سوییچ شما قبول می‌کند، مثلاً `clear ip arp vrf default` یا `clear ip arp vrf all`. در مستندات NX-OS سیسکو که بررسی کردیم، کلمه‌ی `dynamic` برای این دستور وجود ندارد.
- یک بار دستور پاک کردن را دستی اجرا کنید. باید بدون پرسیدن سؤال به prompt برگردد، و ورودی‌های ARP ثابت (static) بعد از آن هنوز باشند.
- همیشه اول روی یک سوییچ امتحان کنید، بعد روی همه.

---

## روش ۱: اسکریپت ساده (`arp_clear_v2.py`)

ساده‌ترین راه شروع: یک فایل، و فهرست سوییچ‌ها داخل خود اسکریپت است.

نیازمندی‌ها: Python 3.10 یا بالاتر و Netmiko 4.x.

### چه کار می‌کند

برای هر سوییچ در `SWITCHES`، یکی‌یکی:

1. با Netmiko از طریق SSH وارد می‌شود (`device_type="cisco_nxos"`).
2. ورودی‌های ARP را با `show ip arp summary` می‌شمارد.
3. با `clear ip arp` آن‌ها را پاک می‌کند.
4. دوباره می‌شمارد.
5. از سوییچ خارج می‌شود، حتی اگر یکی از مراحل خطا داده باشد.
6. پنج ثانیه صبر می‌کند و سراغ سوییچ بعدی می‌رود.

### کد چطور کار می‌کند

| بخش | کارش |
| --- | --- |
| تنظیمات بالای فایل | فهرست سوییچ‌ها، دو دستور، زمان مکث و محل فایل لاگ |
| بخش لاگ | هر پیام را روی صفحه نشان می‌دهد و به `arp_clear.log` کنار اسکریپت اضافه می‌کند |
| `run()` | یک دستور را می‌فرستد (حداکثر ۳۰ ثانیه صبر). اگر خطی از خروجی با `%` شروع شود (یعنی خطای NX-OS)، خطای `CommandRejected` می‌دهد |
| `clear_arp()` | شمارش، پاک کردن و شمارش دوباره را داخل یک بلوک `with` انجام می‌دهد تا اتصال SSH همیشه بسته شود. بعد هر دو خلاصه را در یک خط لاگ می‌کند |
| `get_credentials()` | اگر `NET_USERNAME` و `NET_PASSWORD` تنظیم شده باشند از آن‌ها استفاده می‌کند، وگرنه می‌پرسد (رمز موقع تایپ دیده نمی‌شود) |
| `main()` | روی سوییچ‌ها حلقه می‌زند، هر نوع خطا را به یک پیام کوتاه در لاگ تبدیل می‌کند، و کد خروج `0` (همه موفق) یا `1` (حداقل یک خطا) برمی‌گرداند |

خطای یک سوییچ هرگز کل اجرا را متوقف نمی‌کند. اگر شمارش اول خطا بدهد، آن سوییچ اصلاً پاک نمی‌شود.

### راه‌اندازی

```bash
pip install netmiko
```

تنظیمات بالای `arp_clear_v2.py` را ویرایش کنید:

```python
SWITCHES = ["10.0.0.11"]          # start with one switch
CLEAR_CMD = "clear ip arp"        # as confirmed with: clear ip arp ?
COUNT_CMD = "show ip arp summary"
PAUSE_SECONDS = 5
```

### اجرا

```bash
python3 arp_clear_v2.py
```

اسکریپت نام کاربری و رمز سوییچ را می‌پرسد. برای اینکه نپرسد (مثلاً در اجرای زمان‌بندی‌شده که کسی پای سیستم نیست)، اول آن‌ها را تنظیم کنید:

```bash
export NET_USERNAME='admin'
read -s NET_PASSWORD && export NET_PASSWORD
```

### نتیجه

روی صفحه و در `arp_clear.log` برای هر سوییچ یک خط ثبت می‌شود و در آخر یک جمع‌بندی. برای نمونه:

```text
<time> INFO 10.0.0.11 cleared. before=<summary output> | after=<summary output>
<time> ERROR 10.0.0.12 failed: wrong username or password
<time> INFO Finished: 1 of 2 switches failed
```

### محدودیت‌ها

- اجرای آزمایشی (dry run)، تلاش دوباره و فایل CSV ندارد.
- تعدادها به صورت متن خام ثبت می‌شوند، نه عدد.
- اگر سوییچ سؤال تأیید بپرسد، دستور بعد از ۳۰ ثانیه timeout می‌شود و به اشتباه «در دسترس نبودن سوییچ» ثبت می‌شود.
- فایل لاگ هیچ‌وقت چرخانده (rotate) نمی‌شود و مدام بزرگ‌تر می‌شود.

اگر بیشتر از این لازم دارید، از روش ۲ استفاده کنید.

---

## روش ۲: اسکریپت کامل زمان‌بندی‌شده (`clear_arp.py`)

برای اجرای خودکار روزانه با Windows Task Scheduler ساخته شده است.

نیازمندی‌ها: Python 3.10 یا بالاتر و Netmiko 4.x.

### چه چیزهایی به روش ۱ اضافه می‌کند

- فهرست سوییچ‌ها در `devices.csv` است (یک ستون به نام `host`).
- `--dry-run` فقط می‌شمارد و هرگز پاک نمی‌کند. `--only HOST` فقط روی یک سوییچ از `devices.csv` اجرا می‌شود.
- اگر اتصال برقرار نشد، ۱۰ ثانیه بعد یک بار دیگر تلاش می‌کند.
- به یک سؤال `[confirm]` یا `(y/n)` جواب می‌دهد. دستوری که گیر کند timeout می‌شود و اسکریپت معطل نمی‌ماند.
- خط `Total : N` را می‌خواند. اگر نتواند، خروجی خام را لاگ می‌کند و `unknown` ثبت می‌کند.
- تا وقتی دستوری یا آدرسی هنوز `[براکت]` داشته باشد، اجرا نمی‌شود.
- لاگ چرخشی (۵ مگابایت × ۵ فایل) و برای هر سوییچ در هر اجرا یک ردیف CSV.
- ایمیل اختیاری در صورت خطا (به‌طور پیش‌فرض خاموش).

### راه‌اندازی

1. دو دستور را در `clear_arp.py` بدون براکت بنویسید:

   ```python
   SUMMARY_COMMAND: str = "show ip arp summary"
   CLEAR_COMMAND: str = "clear ip arp vrf default"
   ```

2. سوییچ‌هایتان را در `devices.csv` بنویسید:

   ```text
   host
   10.10.1.11
   10.10.1.12
   ```

3. نام کاربری و رمز را تنظیم کنید:
   - macOS/Linux: `export NET_USERNAME='admin'` و بعد `read -s NET_PASSWORD && export NET_PASSWORD`
   - Windows، با همان حسابی که task با آن اجرا می‌شود: `setx NET_USERNAME "..."` و `setx NET_PASSWORD "..."`

### اجرا، به همین ترتیب

```bash
python3 clear_arp.py --dry-run           # count only, nothing is cleared
python3 clear_arp.py --only 10.10.1.11   # one switch
python3 clear_arp.py                     # every switch in devices.csv
```

### زمان‌بندی روزانه ساعت ۳ بامداد (Windows)

```text
schtasks /Create /F /TN "NXOS-ARP-Clear" /SC DAILY /ST 03:00 /RU "[DOMAIN\user]" /RP "[windows-password]" /TR "[C:\path\to\python.exe] [C:\path\to\clear_arp.py]"
schtasks /Run /TN "NXOS-ARP-Clear"
schtasks /Query /TN "NXOS-ARP-Clear" /V /FO LIST
```

- `/RU` باید همان حسابی باشد که `NET_USERNAME` و `NET_PASSWORD` برایش تنظیم شده است.
- از پوشه‌های بدون فاصله استفاده کنید تا `/TR` به نقل‌قول اضافه نیاز نداشته باشد.

### نتیجه

| فایل | محتوا |
| --- | --- |
| `logs/clear_arp.log` | همه‌ی مراحل برای همه‌ی سوییچ‌ها |
| `arp_clear_results.csv` | `timestamp,host,status,entries_before,entries_after,error` برای اجراهای واقعی |
| `arp_clear_dryrun.csv` | همان ستون‌ها، برای اجراهای آزمایشی |

کد خروج فقط وقتی `0` است که همه‌ی سوییچ‌ها موفق باشند، وگرنه `1` (در Task Scheduler به شکل `0x1` دیده می‌شود).

### تست‌ها (بدون نیاز به سوییچ واقعی)

پوشه‌ی `tests/` یک سرور SSH شبیه‌ساز NX-OS و یک تست کامل برای `clear_arp.py` دارد:

```bash
python3 tests/run_sim_tests.py
```

برای هر بررسی `PASS` یا `FAIL` چاپ می‌کند و در آخر `N/M checks passed`. فقط روی Linux تست شده است. در macOS به‌طور پیش‌فرض فقط `127.0.0.1` وجود دارد، پس اول آدرس‌هایی را که تست استفاده می‌کند (127.0.0.11 تا 127.0.0.16) اضافه کنید، مثلاً `sudo ifconfig lo0 alias 127.0.0.11`.

---

## روش ۳: روی خود سوییچ، بدون پایتون (`nxos_scheduler_arp_clear.txt`)

از زمان‌بند داخلی NX-OS استفاده می‌کند، پس هر سوییچ جدول ARP خودش را پاک می‌کند. هیچ چیزی بیرون از سوییچ اجرا نمی‌شود.

### چه کار می‌کند

هر روز ساعت ۳ بامداد (به ساعت سوییچ)، یک job زمان‌بند:

1. زمان و خروجی `show ip arp summary` را به `bootflash:arp_clear_history.txt` اضافه می‌کند.
2. دستور `clear ip arp vrf default` را اجرا می‌کند.
3. دوباره خروجی `show ip arp summary` را اضافه می‌کند.

### راه‌اندازی

سه مرحله‌ی فایل `nxos_scheduler_arp_clear.txt` را دنبال کنید: دستورها را دستی امتحان کنید، پیکربندی را روی هر سوییچ paste کنید، بعد با یک زمان‌بندی موقت تست کنید. پیکربندی اصلی این است:

```text
configure terminal
feature scheduler
scheduler logfile size 1024
scheduler job name ARP-CLEAR
show clock >> bootflash:arp_clear_history.txt ; show ip arp summary >> bootflash:arp_clear_history.txt ; clear ip arp vrf default ; show ip arp summary >> bootflash:arp_clear_history.txt
exit
scheduler schedule name ARP-CLEAR-DAILY
job name ARP-CLEAR
time daily 03:00
exit
end
copy running-config startup-config
```

- اگر با TACACS+ یا RADIUS وارد می‌شوید، اول در حالت config این را اجرا کنید: `scheduler aaa-authentication password 0 [your password]`. هر بار رمزتان عوض شد، دوباره واردش کنید.
- برای پاک کردن همه‌ی VRFها، در دستور پاک کردن و هر دو دستور show از `vrf all` استفاده کنید.

### دیدن نتیجه

```text
show scheduler schedule
show scheduler logfile
show file bootflash:arp_clear_history.txt
```

### محدودیت‌ها

- همه‌ی سوییچ‌ها در یک لحظه پاک می‌شوند. برای هر سوییچ، به‌خصوص دو سوییچ یک جفت vPC، زمان متفاوتی بگذارید.
- شمارش عددی، CSV، ایمیل، تلاش دوباره و جواب دادن به سؤال ندارد. نتیجه روی هر سوییچ می‌ماند.
- فایل تاریخچه مدام بزرگ‌تر می‌شود، پس هر از گاهی پاکش کنید.

---

## ساختار مخزن

```text
.
├── README.md
├── arp_clear_v2.py               # Method 1: simple script
├── clear_arp.py                  # Method 2: full scheduled script
├── devices.csv                   # Method 2: switch list
├── tests/
│   ├── sim_nxos.py               # fake NX-OS SSH server
│   └── run_sim_tests.py          # end-to-end tests for clear_arp.py
└── nxos_scheduler_arp_clear.txt  # Method 3: on-switch scheduler config
```

## امنیت

- هرگز رمز را commit نکنید. اسکریپت‌ها رمز را از متغیرهای محیطی می‌خوانند یا از شما می‌پرسند.
- فایل‌های لاگ و نتیجه را commit نکنید، چون آدرس سوییچ‌ها در آن‌هاست. `.gitignore` پیشنهادی:

  ```text
  logs/
  *.log
  arp_clear_results.csv
  arp_clear_dryrun.csv
  ```

## منابع

- [Netmiko](https://github.com/ktbyers/netmiko)
- [Cisco Nexus 3000 NX-OS System Management Configuration Guide 9.3(x): Configuring the Scheduler](https://www.cisco.com/c/en/us/td/docs/switches/datacenter/nexus3000/sw/system_mgmt/93x/system-management/b-cisco-nexus-3000-series-nx-os-system-management-configuration-guide-93x/b-cisco-nexus-3000-series-nx-os-system-management-configuration-guide-93x_chapter_01011.html)
- [Cisco Nexus 9000 NX-OS Configuration Commands 7.x: `clear ip arp`](https://www.cisco.com/c/en/us/td/docs/switches/datacenter/nexus9000/sw/7-x/command_references/configuration_commands/b_Using_N9K_Config_Commands/b_N9K_Bookmap_chapter_010.html)
- [Cisco Nexus 9000 NX-API CLI Reference: IP commands (`show ip arp summary`)](https://developer.cisco.com/docs/cisco-nexus-9000-series-nx-api-cli-reference/latest/ip-commands)
