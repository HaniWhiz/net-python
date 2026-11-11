# پاک‌سازی روزانهٔ MAC Table روی Cisco Nexus با NX-OS Scheduler

اسکریپت `clear-dmac-schedule.py` از طریق Jump Server (یا مستقیم) به سوییچ‌های Cisco Nexus وصل می‌شود و روی هر سوییچ این کارها را انجام می‌دهد:

1. همان لحظه `clear mac address-table dynamic` را اجرا می‌کند.
2. یک job در NX-OS Scheduler به نام `MAC-CLEAR` و یک schedule به نام `MAC-CLEAR-DAILY` می‌سازد که هر روز در ساعت تعیین‌شده (پیش‌فرض `04:05` به وقت خود سوییچ) MAC Table را پاک می‌کند.
3. job و schedule را با `show scheduler ...` بررسی می‌کند.
4. با `copy running-config startup-config` تنظیمات را ذخیره می‌کند.

اسکریپت فقط یک بار اجرا می‌شود. پاک‌سازی روزانه را خود سوییچ انجام می‌دهد، پس لازم نیست کامپیوتر یا اسکریپت روشن بماند.

## فایل‌ها

| فایل | توضیح |
| --- | --- |
| `clear-dmac-schedule.py` | خود اسکریپت |
| `switches.yaml` | Jump Server، تنظیمات ورود و تنظیمات scheduler |
| `forough.csv` | (اختیاری) فهرست سوییچ‌ها، ستون اول IP یا hostname |
| `nxos_scheduler_mac_clear.txt` | همان config برای paste دستی روی سوییچ، بدون Python |

همهٔ فایل‌ها باید در یک پوشه باشند.

## نصب

Python 3.10 یا بالاتر لازم است:

```bash
pip install paramiko pyyaml
```

## تنظیم `switches.yaml`

```yaml
auth:
  username: admin
  use_agent: true        # کلیدهای SSH agent (یوبی‌کی)
  key_files: []
  passphrase: ""
  password: ""

# اگر Jump Server ندارید، کل بخش jump را پاک کنید
jump:
  host: 192.0.2.10
  username: myuser
  use_agent: true

scheduler:
  time: "04:05"          # به وقت خود سوییچ (show clock)، فرمت HH:MM
  aaa_password: ""       # فقط اگر با TACACS+/RADIUS وارد می‌شوید

# فقط اگر forough.csv وجود نداشته باشد استفاده می‌شود
hosts:
  - 10.0.0.11
  - 10.0.0.12
  - host: 10.0.0.13
    username: netops
    use_agent: false
    key_files: ['~/.ssh/lab_key']
```

| تنظیم | معنی |
| --- | --- |
| `auth` | تنظیمات پیش‌فرض ورود به سوییچ‌ها. `username` الزامی است |
| `jump` | Jump Server و تنظیمات ورود به آن |
| `scheduler.time` | ساعت اجرای روزانه روی سوییچ |
| `scheduler.aaa_password` | رمز ورود شما برای `scheduler aaa-authentication` (توضیح پایین) |
| `hosts` | فهرست سوییچ‌ها، اگر فایل CSV نباشد. هر سوییچ می‌تواند تنظیم ورود خودش را داشته باشد |
| `use_agent` | `true` یعنی از کلیدهای SSH agent (یوبی‌کی) استفاده شود |
| `key_files` | فایل‌های کلید دیگر، مثلاً `['~/.ssh/id_ed25519']` |
| `password` | رمز عبور به صورت متن ساده. `""` یعنی استفاده نشود |

## فهرست سوییچ‌ها در `forough.csv`

اگر این فایل باشد، به جای `hosts` از آن استفاده می‌شود:

- فقط ستون اول خوانده می‌شود. جداکننده می‌تواند `,` یا `;` یا Tab باشد.
- سطر عنوان (مثل `IP Address` یا `Hostname`) و سطرهایی که با `#` شروع می‌شوند نادیده گرفته می‌شوند.
- ماسک مثل `/24` حذف می‌شود و سوییچ تکراری فقط یک بار وصل می‌شود.

## اجرا

```bash
python clear-dmac-schedule.py
```

قبل از اجرا، یوبی‌کی باید در SSH agent باشد (`ssh-add -l`). اسکریپت مثل `ssh -J` کار می‌کند: یک بار به Jump Server وارد می‌شود، برای هر سوییچ یک تونل باز می‌کند، کار را انجام می‌دهد و خارج می‌شود. Jump Server باید TCP forwarding را اجازه دهد. تست سریع:

```bash
ssh -J myuser@192.0.2.10 admin@10.0.0.11
```

## config‌ای که روی سوییچ ساخته می‌شود

```
configure terminal
feature scheduler
scheduler logfile size 1024
no scheduler schedule name MAC-CLEAR-DAILY
no scheduler job name MAC-CLEAR
scheduler job name MAC-CLEAR
show clock >> bootflash:mac_clear_history.txt
show mac address-table count >> bootflash:mac_clear_history.txt
clear mac address-table dynamic
show mac address-table count >> bootflash:mac_clear_history.txt
exit
scheduler schedule name MAC-CLEAR-DAILY
job name MAC-CLEAR
time daily 04:05
exit
end
copy running-config startup-config
```

- **اجرای دوباره امن است:** در NX-OS دستورهای job قابل ویرایش نیستند و وارد شدن دوباره به یک job موجود، دستورها را به انتهای آن **اضافه** می‌کند. برای همین اسکریپت اول schedule و job قبلی را پاک می‌کند و بعد دوباره می‌سازد. خطای دو خط `no ...` وقتی job هنوز وجود ندارد طبیعی است و نادیده گرفته می‌شود.
- **بررسی قبل از ذخیره:** `show scheduler job name MAC-CLEAR` باید `clear mac address-table dynamic` را دقیقاً یک بار نشان دهد و `show scheduler schedule name MAC-CLEAR-DAILY` باید job را نشان دهد. اگر نه، config ذخیره نمی‌شود.
- **بدون `>>`:** اگر سوییچی `>>` را داخل job قبول نکند (بعضی نسخه‌های قدیمی N3K)، job فقط `clear mac address-table dynamic` را اجرا می‌کند و فایل تاریخچه ساخته نمی‌شود.

## TACACS+ / RADIUS

job به نام کاربری اجرا می‌شود که آن را ساخته. اگر با TACACS+/RADIUS وارد می‌شوید، بدون `scheduler aaa-authentication` اجرای شبانه بی‌صدا شکست می‌خورد. رمز ورودتان را در `scheduler.aaa_password` بگذارید. هر بار رمزتان عوض شد، اسکریپت را دوباره اجرا کنید. رمز در خروجی اسکریپت با `******` نمایش داده می‌شود.

## بررسی روی سوییچ بعد از اولین اجرا

```
show scheduler schedule
show scheduler logfile
show file bootflash:mac_clear_history.txt
```

`show scheduler logfile` برای هر اجرا Success یا Failed را نشان می‌دهد. فایل تاریخچه زمان و تعداد MAC قبل و بعد از پاک‌سازی را دارد.

## خطاها

- اگر ورود به Jump Server شکست بخورد، اسکریپت متوقف می‌شود.
- اگر یک سوییچ خطا بدهد، خطا و خروجی سوییچ چاپ می‌شود و بقیهٔ سوییچ‌ها ادامه پیدا می‌کنند.

## نکته‌ها

- همهٔ سوییچ‌ها در یک لحظه پاک‌سازی می‌کنند. برای vPC peerها یا سوییچ‌های حساس، ساعت متفاوت بگذارید (مثلاً اسکریپت را با `time` متفاوت برای هر گروه اجرا کنید).
- فایل تاریخچه هر روز چند خط بزرگ‌تر می‌شود. گاهی آن را پاک کنید. scheduler log حداکثر 1024 KB است.
- رمزها در `switches.yaml` متن ساده‌اند. این فایل را خصوصی نگه دارید و رمز واقعی را در GitHub نگذارید.

---

# اسکریپت عمومی: `net-deploy.py` + `net-deploy.yaml`

برای هر کار دیگری (نه فقط MAC clear) از این جفت استفاده کنید. در فایل `.py` هیچ دستور Cisco یا نام فایلی نوشته نشده است. همه چیز در فایل YAML است: تنظیمات ورود، Jump Server، فایل CSV ورودی و دستورهایی که باید روی سوییچ‌ها اجرا شوند.

برای هر کار جدید، `net-deploy.yaml` را کپی کنید، دستورها و CSV را عوض کنید و اجرا کنید:

```bash
python net-deploy.py                          # از net-deploy.yaml کنار اسکریپت استفاده می‌کند
python net-deploy.py -c vlan-job.yaml --dry-run   # فقط نمایش سوییچ‌ها و دستورها، چیزی فرستاده نمی‌شود
python net-deploy.py -c vlan-job.yaml
```

## بخش‌های فایل YAML

| بخش | معنی |
| --- | --- |
| `name` | نام کار. در خروجی و نام فایل‌های log استفاده می‌شود |
| `auth` | ورود به سوییچ‌ها (`username`، `use_agent`، `key_files`، `password`، `port`) |
| `jump` | Jump Server. اگر ندارید کل بخش را پاک کنید |
| `hosts_csv` | فایل CSV ورودی. ستون اول IP یا hostname. مسیر نسبت به خود فایل YAML است |
| `hosts` | سوییچ‌های اضافه که داخل YAML نوشته می‌شوند. می‌تواند همراه `hosts_csv` باشد |
| `settings` | `command_timeout`، `stop_on_error`، `disable_paging`، `log_dir` |
| `commands` | دستورها، به همان ترتیب روی هر سوییچ فرستاده می‌شوند |

## دستورها

هر دستور یا یک خط ساده است، یا یک بلوک با گزینه:

```yaml
commands:
  - configure terminal
  - cmd: no scheduler job name MAC-CLEAR
    ignore_errors: true          # خطای این دستور کار را متوقف نمی‌کند
  - cmd: show vlan brief
    show: true                   # خروجی روی صفحه چاپ شود
  - cmd: copy running-config startup-config
    timeout: 90                  # این دستور بیشتر طول می‌کشد
    expect: Copy complete        # این متن باید در خروجی باشد
  - cmd: username test password 0 MyPass
    secret: true                 # دستور در خروجی و log مخفی می‌شود
  - cmd: reload in 10
    answer: "y"                  # جواب سؤال (y/n) یا [confirm]
```

- اگر دستوری خطا بدهد، همان سوییچ متوقف می‌شود (`stop_on_error: true`) و اسکریپت با `end` از config mode خارج می‌شود. بقیهٔ سوییچ‌ها ادامه پیدا می‌کنند.
- اگر `answer` ننویسید: برای `[confirm]` فقط Enter فرستاده می‌شود. برای سؤال `(y/n)` جواب `n` فرستاده می‌شود و دستور خطا حساب می‌شود، تا چیزی بدون اجازهٔ شما تأیید نشود.
- در آخر خلاصهٔ OK / FAILED چاپ می‌شود. اگر سوییچی خطا داشته باشد، exit code برابر `1` است.
- با `log_dir: logs` خروجی کامل هر سوییچ در یک فایل ذخیره می‌شود.

`net-deploy.yaml` نمونه همان MAC-CLEAR scheduler را دارد. تفاوت با `clear-dmac-schedule.py`: اسکریپت عمومی اگر سوییچی `>>` را قبول نکند خودش job را بدون فایل تاریخچه نمی‌سازد و روی همان دستور متوقف می‌شود.

---

# اسکریپت عمومی با فایل دستور جدا: `net-push.py` + `net-push.yaml` + `net-push.txt`

همان `net-deploy.py` است، با این تفاوت که دستورها به جای YAML در یک فایل `.txt` جدا نوشته می‌شوند:

| فایل | محتوا |
| --- | --- |
| `net-push.py` | اسکریپت. هیچ دستور Cisco یا نام فایلی در آن نیست |
| `net-push.yaml` | تنظیمات ورود، Jump Server، نام فایل CSV ورودی و نام فایل دستورها |
| `net-push.txt` | دستورهایی که روی سوییچ‌ها اجرا می‌شوند، هر خط یک دستور |

برای هر کار جدید، `.yaml` و `.txt` را کپی کنید، دستورها و CSV را عوض کنید و اجرا کنید:

```bash
python net-push.py                              # از net-push.yaml کنار اسکریپت استفاده می‌کند
python net-push.py -c vlan-job.yaml --dry-run   # فقط نمایش سوییچ‌ها و دستورها، چیزی فرستاده نمی‌شود
python net-push.py -c vlan-job.yaml
```

## بخش‌های فایل YAML

| بخش | معنی |
| --- | --- |
| `name` | نام کار. در خروجی و نام فایل‌های log استفاده می‌شود |
| `auth` | ورود به سوییچ‌ها (`username`، `use_agent`، `key_files`، `password`، `port`) |
| `jump` | Jump Server. اگر ندارید کل بخش را پاک کنید |
| `hosts_csv` | نام فایل CSV ورودی. ستون اول IP یا hostname. مسیر نسبت به خود فایل YAML است |
| `hosts` | سوییچ‌های اضافه که داخل YAML نوشته می‌شوند. می‌تواند همراه `hosts_csv` باشد |
| `commands_file` | نام فایل `.txt` دستورها. مسیر نسبت به خود فایل YAML است |
| `settings` | `command_timeout`، `stop_on_error`، `disable_paging`، `log_dir` |

## فایل دستورها (`.txt`)

هر خط یک دستور است و به همان ترتیب روی هر سوییچ فرستاده می‌شود. خط خالی و خطی که با `#` یا `!` شروع شود نادیده گرفته می‌شود. گزینه‌ها آخر خط بعد از `##` می‌آیند و با `,` جدا می‌شوند:

```
configure terminal
no scheduler job name MAC-CLEAR          ## ignore_errors
show vlan brief                          ## show
copy running-config startup-config       ## timeout=90, expect=Copy complete
username test password 0 MyPass          ## secret
reload in 10                             ## answer=y
```

| گزینه | معنی |
| --- | --- |
| `ignore_errors` | خطای این دستور کار را متوقف نمی‌کند |
| `timeout=90` | این دستور بیشتر طول می‌کشد (ثانیه) |
| `expect=متن` | این متن باید در خروجی باشد |
| `answer=y` | جواب سؤال `(y/n)` یا `[confirm]` |
| `show` | خروجی روی صفحه چاپ شود |
| `secret` | دستور در خروجی و log مخفی می‌شود |

- اگر دستوری خطا بدهد، همان سوییچ متوقف می‌شود (`stop_on_error: true`) و اسکریپت با `end` از config mode خارج می‌شود. بقیهٔ سوییچ‌ها ادامه پیدا می‌کنند.
- اگر `answer` ننویسید: برای `[confirm]` فقط Enter فرستاده می‌شود. برای سؤال `(y/n)` جواب `n` فرستاده می‌شود و دستور خطا حساب می‌شود، تا چیزی بدون اجازهٔ شما تأیید نشود.
- در آخر خلاصهٔ OK / FAILED چاپ می‌شود. اگر سوییچی خطا داشته باشد، exit code برابر `1` است.
- با `log_dir: logs` خروجی کامل هر سوییچ در یک فایل ذخیره می‌شود.

`net-push.txt` نمونه همان MAC-CLEAR scheduler را دارد. تفاوت با `clear-dmac-schedule.py`: اسکریپت عمومی اگر سوییچی `>>` را قبول نکند خودش job را بدون فایل تاریخچه نمی‌سازد و روی همان دستور متوقف می‌شود.
