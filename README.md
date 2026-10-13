# MGlogWH Control Room

MGlogWH 수집기의 실행 상태와 로그를 로컬 브라우저에서 보는 관제 화면입니다. **WSL용**과 **일반 Linux 서버용** 설치 스크립트가 있으며 같은 Python 웹 앱을 사용합니다. 수집기와 별도 저장소이고, 수집기를 설치하거나 자동으로 시작하지 않습니다.

## 화면에서 할 수 있는 일

- 약 1초 간격의 새 이벤트 표시. Critical, Normal, Bulk를 색과 정책 배지로 구분하고 검색·등급 필터·일시정지·상세 보기·CSV 내보내기를 제공합니다.
- 수집기 상태, 인덱스 지연, 손실 관련 상태, 누적 이벤트 수를 봅니다. 실행 기록을 고르고 해당 실행의 파일 목록을 확인·다운로드하거나 **서버 경로**를 복사할 수 있습니다. `저장 폴더 내용 보기`는 웹 화면 안의 파일 목록입니다. 브라우저를 연 PC의 파일 관리자를 실행하지는 않습니다.
- 기존 `critical_rules`는 간단한 경로 폼에서 편집합니다. 새 MGlogWH의 `event_rules`는 조건식 JSON 편집기에서 `all`/`any`/`not`, Priority, Overload Policy와 규칙별 Quarantine 값을 설정할 수 있습니다. 두 스키마는 동시에 저장하지 않습니다.
- 수집기가 없는 컴퓨터에서도 `--demo`로 합성 이벤트와 화면 흐름을 볼 수 있습니다. 데모 화면의 로그·규칙은 실제 수집 결과가 아니며 실제 서비스를 재시작하지 않습니다.

Critical은 설정 규칙에 따른 **중요도 분류**이며 공격 판정이 아닙니다. 화면의 이벤트 시각과 새 로그 표시는 대략적인 값입니다. 검색 DB는 raw에서 파생되므로 인덱스가 늦거나 실패하면 표시가 늦거나 비어 있을 수 있습니다. 원본·손실 해석에는 MGlogWH의 `health.json`, raw, `evidence.meta`와 복구 결과를 함께 확인하세요.

규칙 편집은 `/api/rules`에서 현재 스키마를 명시합니다. 레거시 화면의 **고급 규칙 편집 열기**는 기존 규칙을 `event_type`과 `path`의 AND 조건으로 변환한 초안만 보여줍니다. JSON 저장 시 `critical_rules`를 제거하고 `event_rules`로 전환하며, MGlogWH의 `manage.py validate`가 설정 전체를 검사합니다. 여러 규칙이 동시에 일치할 때는 새 엔진의 명시적 우선순위가 적용되므로 전환 전에 규칙을 확인하세요. 구버전 수집기는 `event_rules` 검증을 거부하며 기존 설정을 유지합니다. 데모의 JSON 검사는 구조 확인 수준이고 실제 엔진 유효성을 보증하지 않습니다.

새 규칙 형식은 `{"id":1,"when":{"all":[{"field":"event_type","op":"eq","value":"openat"},{"field":"path","op":"eq","value":"/etc/shadow"}]},"priority":"CRITICAL","overload_policy":"QUARANTINE_EXEMPT"}`처럼 지정합니다. 규칙은 최대 8개, 규칙마다 조건 노드와 실행 토큰은 각각 최대 24개, 조건 깊이는 최대 4단계이며 수집기 설정 한도는 64 KiB입니다. `in`·`not_in` 목록은 최대 4개 값입니다. 자세한 필드·연산자·제한과 A~D 예제는 [MGlogWH RULES.md](https://github.com/habang2222/MGlogWH/blob/main/RULES.md)를 확인하세요. 새 엔진은 Raw schema 3으로 기록하고 이전 run은 기존 Reader로 읽습니다. 이벤트 상세에는 수집기가 제공한 최종 규칙 ID, 원래 중요도, 설정된 폭주 정책, 실제 처리 상태, 규칙·정책 버전을 각각 표시합니다. 필드가 없는 이전 run에는 값을 추정해 채우지 않습니다. 설정을 저장한 뒤 실행 중인 수집기에는 바로 반영되지 않으며, 적용 단계에서 새 run으로 다시 시작해야 합니다.

규칙별 관측 통계는 현재 run의 `health.json`/`manage.py status`가 제공하는 값만 보여줍니다. `matched`는 조건에 일치한 모든 규칙, `selected`는 충돌 해결 후 최종 선택된 규칙의 횟수입니다. 통계가 없거나 `null`인 값은 `UNKNOWN`으로 표시합니다. 스냅샷 완료 표시도 전체 이벤트의 무손실 보증은 아닙니다. 사용자 공간 평가 이전의 Ring Buffer 유실은 규칙별로 귀속할 수 없을 수 있습니다.

`QUARANTINE_EXEMPT`는 격리 중 요약·샘플링을 피하는 설정입니다. Ring Buffer, 큐, 저장 공간 등의 실패까지 방지하지 않으므로 로그 무손실 보장이 아닙니다.

파일 목록은 실행 폴더의 알려진 파일을 보여줍니다. 256 MiB를 넘는 파일과 수집 중인 raw·SQLite 파일은 웹에서 내려받을 수 없습니다. 서버의 원래 경로에서 직접 확인할 수 있습니다. WSL의 수집기 폴더는 보통 root 전용이어서 Windows 탐색기로 여는 방식은 제공하지 않습니다.

## 요구 사항과 저장 위치

- Linux의 `/usr/bin/python3` **3.12 이상**, 표준 라이브러리, `bash`, GNU coreutils. 별도 Python 패키지 설치가 필요하지 않습니다.
- 실제 로그와 규칙을 쓰려면 [MGlogWH 수집기](https://github.com/habang2222/MGlogWH)가 필요합니다. 기본 위치는 코드 `/opt/mglogwh`, 실행 자료 `/var/lib/mglogwh`, 설정 `/etc/mglogwh/config.json`입니다. 수집기가 없거나 중지되어도 관제 화면은 연결되지 않은 상태를 표시합니다.
- Control Room 앱은 `/opt/mglogwh-controlroom`, 실행 명령은 `/usr/local/bin/mglogwh-controlroom`, 로그인 토큰과 모드는 `/etc/mglogwh-controlroom/`에 설치합니다. 기존 토큰은 업데이트해도 유지합니다. 수집기 자료와 설정은 설치·업데이트·삭제 과정에서 건드리지 않습니다.
- 로그에는 민감한 경로와 프로세스 정보가 있을 수 있어 서비스는 root로 읽고 **127.0.0.1:8765**에만 바인딩합니다. 32바이트 이상의 무작위 토큰을 0600 권한으로 보관합니다. 서버 외부에서 볼 때는 아래 SSH 터널을 사용하세요.

## WSL 설치

Windows PowerShell에서 `wsl -l -v`로 배포판을 확인한 다음 Ubuntu 24.04 WSL 터미널을 엽니다. Ubuntu 안에서 실행합니다.

```bash
sudo apt update
sudo apt install -y git python3
git clone https://github.com/habang2222/MGlogWH-ControlRoom.git
cd MGlogWH-ControlRoom
sudo bash scripts/install-wsl.sh
bash scripts/open-wsl.sh
```

마지막 명령은 Windows 기본 브라우저에서 `http://127.0.0.1:8765`를 엽니다. 브라우저 로그인 칸에는 Ubuntu에서 `sudo cat /etc/mglogwh-controlroom/token`으로 확인한 토큰을 붙여 넣습니다. URL에 토큰을 넣지 않습니다. Windows에서 localhost 전달이 꺼져 있다면 WSL 네트워크 설정에 맞는 전달 방법이 필요합니다.

WSL에서 systemd가 실행 중이면 설치기가 `mglogwh-controlroom.service`를 활성화하고 시작합니다. systemd가 없으면 WSL 터미널에서 `sudo mglogwh-controlroom`을 실행한 채 브라우저를 엽니다. WSL을 종료하면 systemd 없는 수동 실행도 종료됩니다.

**수집기가 없는 WSL에서 화면만 먼저 확인**하려면 저장소 폴더에서 아래 명령을 일반 사용자로 직접 실행할 수 있습니다. 설치를 하지 않고 합성 로그를 표시합니다. 실행 중인 터미널은 그대로 두고 다른 WSL 터미널에서 `bash scripts/open-wsl.sh "$HOME/.config/mglogwh-controlroom-demo/token"`을 실행하고 `cat ~/.config/mglogwh-controlroom-demo/token`으로 로그인 토큰을 확인하세요.

```bash
cd MGlogWH-ControlRoom
python3 -m controlroom --mode wsl --host 127.0.0.1 --port 8765 \
  --token-file "$HOME/.config/mglogwh-controlroom-demo/token" --demo
```

## 일반 Linux·Ubuntu 서버 설치

서버에서 다음 명령을 실행합니다. 수집기를 다른 서버에 따로 설치해도 이 화면은 **현재 서버의 로컬 파일**을 읽으므로, 수집기가 있는 서버에 설치하세요.

```bash
sudo apt update
sudo apt install -y git python3
git clone https://github.com/habang2222/MGlogWH-ControlRoom.git
cd MGlogWH-ControlRoom
sudo bash scripts/install-linux.sh
sudo systemctl status mglogwh-controlroom --no-pager
```

`apt` 예시는 Ubuntu용입니다. 다른 Linux 배포판에서는 해당 패키지 관리자로 Python 3.12 이상과 git을 설치하세요. systemd가 없으면 `sudo mglogwh-controlroom`으로 수동 실행합니다. 서버 자체의 브라우저에서는 `http://127.0.0.1:8765`에 접속합니다.

원격 서버를 내 PC에서 볼 때는 **내 PC에서** 터널을 만듭니다. `user`, `server`를 실제 계정과 호스트로 바꾸세요.

```bash
ssh -L 8765:127.0.0.1:8765 user@server
```

터널을 유지한 채 내 PC 브라우저에서 `http://127.0.0.1:8765`를 열고 서버의 `sudo cat /etc/mglogwh-controlroom/token`으로 확인한 토큰으로 로그인합니다. 서버의 8765 포트를 외부에 개방할 필요가 없습니다.

## 사용·운영

규칙을 저장하면 `/etc/mglogwh/config.json`이 바뀝니다. 현재 실행 중인 수집기의 정책은 즉시 바뀌지 않습니다. 화면의 별도 **적용** 버튼은 `mglogwh.service`를 재시작합니다. systemd 서비스가 있을 때만 이 버튼을 사용할 수 있습니다. 서버에서 직접 적용하려면 다음 명령을 실행합니다.

```bash
sudo systemctl restart mglogwh
sudo systemctl status mglogwh --no-pager
```

재시작은 새 run을 만들 수 있으므로 수집기 상태와 실행 기록을 확인하세요. 수집기 설정과 로그에 접근할 수 없는 경우에는 화면에 오류 또는 연결되지 않은 상태가 표시됩니다. 규칙을 편집할 때는 MGlogWH 자체의 설정 검증을 통과해야 합니다.

WSL 등 systemd가 없는 환경에서 MGlogWH 수집기를 수동으로 실행 중이라면 적용 버튼이 오류를 표시해도 저장한 설정은 유지됩니다. 수집기의 `manage.py` 명령으로 직접 중지·시작하여 새 설정을 적용하세요.

```bash
sudo python3 /opt/mglogwh/manage.py stop --config /etc/mglogwh/config.json
sudo python3 /opt/mglogwh/manage.py start --config /etc/mglogwh/config.json
```

Control Room 상태와 오류는 다음처럼 확인합니다.

```bash
sudo systemctl status mglogwh-controlroom --no-pager
sudo journalctl -u mglogwh-controlroom -n 100 --no-pager
```

수집기 위치를 바꿨거나 systemd가 없는 환경에서는 설치 명령의 옵션을 사용할 수 있습니다. 기본값은 위 표의 경로입니다.

```bash
sudo mglogwh-controlroom --sensor-root /custom/mglogwh \
  --data-dir /custom/mglogwh-data --config /custom/config.json
```

기본 서비스는 127.0.0.1:8765에서 동작합니다. 서비스 설정을 변경하려면 `sudo systemctl edit mglogwh-controlroom`으로 override를 만들고 `ExecStart=`를 비운 뒤 필요한 전체 시작 명령을 지정하세요. 서비스 변경 후 `sudo systemctl daemon-reload && sudo systemctl restart mglogwh-controlroom`을 실행합니다.

### 데모 모드

설치 후 8765 포트의 실제 화면은 그대로 두고 8766 포트에서 데모를 열 수 있습니다.

```bash
sudo mglogwh-controlroom --demo --port 8766
```

`http://127.0.0.1:8766`에 접속해 같은 로그인 토큰을 사용합니다. 데모 이벤트와 규칙은 합성 데이터입니다.

### 업데이트와 삭제

같은 설치 유형의 스크립트를 다시 실행하면 앱을 교체하고 서비스를 재시작합니다. 토큰과 MGlogWH 수집기 설정·로그는 유지합니다.
systemd 없이 `sudo mglogwh-controlroom`으로 직접 실행했다면 해당 프로세스를 `Ctrl+C`로 종료한 다음 업데이트하거나 삭제하세요.

```bash
cd MGlogWH-ControlRoom
git pull --ff-only
sudo bash scripts/install-linux.sh  # WSL은 install-wsl.sh
```

관제 앱만 제거하려면 다음 명령을 사용합니다. 설치한 앱·서비스·토큰만 제거하고 MGlogWH 수집기와 데이터는 그대로 둡니다.

```bash
sudo bash scripts/uninstall.sh
```

## 프로젝트 구조와 범위

`controlroom/`은 Python 서버, `web/`은 브라우저 화면, `scripts/`는 설치·제거·WSL 브라우저 실행 도구, `packaging/`은 서비스와 실행 명령입니다. 이 프로그램은 MGlogWH 수집기의 기존 raw·인덱스·설정 파일을 다룹니다. 인덱스가 누락한 원본을 복원하거나 로그 진위를 암호학적으로 증명하는 기능은 아닙니다.

현재 이 저장소에는 최상위 LICENSE가 없습니다. 재배포와 상용 사용 조건은 저장소 소유자가 정해야 합니다.
