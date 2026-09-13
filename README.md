# 업무 자동화 Core — 승인 없이는 실행하지 않는 사무 자동화 파이프라인

[![CI](https://github.com/sn-owl/automation-webs/actions/workflows/ci.yml/badge.svg)](https://github.com/sn-owl/automation-webs/actions/workflows/ci.yml)

서로 다른 출처에 흩어져 있는 반복 업무를 **한 곳으로 수집하고, 같은 계약으로 분류하고,
사람이 승인한 것만 준비 수행**하는 파이프라인이다. 외부 모델은 제안만 하고, 실행 권한은
언제나 코드와 사람에게 있다.

- **Python 표준 라이브러리만으로 동작하는 Core** — 설치 없이 클론 후 바로 테스트가 돈다
- **697개 테스트** + Chrome Extension 테스트 5개 + 공개 안전 게이트
- 원본 파일은 절대 수정하지 않는다. 산출물은 항상 **새 파일**이다

---

## 무엇을 푸는가

담당자 한 사람에게 들어오는 업무는 보통 한 곳에서 오지 않는다. 웹 게시판, 메신저,
메일, 이슈 트래커에 흩어져 있고, 각각 로그인 방식도 알림 방식도 다르다.
그래서 매일 이런 일이 반복된다.

1. 출처를 하나씩 돌며 **새 업무가 들어왔는지 수집한다**
2. 수집한 업무를 읽고 담당인지, 어느 유형인지 판단한다
3. 반복되는 유형(예: 신청서 HWPX → 업로드용 XLS 변환)은 손으로 같은 작업을 되풀이한다
4. 처리 결과를 기록하고 알린다

1·2·4는 기계가 도울 수 있고, 3은 **자동화해도 되는지부터 판단해야** 한다.
이 저장소는 그 판단 경계를 코드로 고정한 결과물이다.

수집 방식은 출처마다 다르지만, 수집된 결과는 **공통 WorkItem 하나의 형태**로 Core에
넘어간다. 그래서 새 출처를 붙여도 분류·평가·실행 계약은 바뀌지 않는다.

핵심 제약은 자동화율이 아니라 **안전**이다.

> 허용된 자동 준비도 ① 승인된 패턴, ② 읽기 권한, ③ 새 파일 출력 경계, ④ 검증을 모두 통과해야 한다.

## 설계 원칙

| 원칙 | 코드에서의 의미 |
|---|---|
| 원본 불변 | 기존 파일은 read-only. 산출물은 `artifacts/<task>/result-N/attempt-N/` 아래 새 파일 |
| 승인 우선 | 등록되지 않은 Recipe는 어떤 경로로도 실행되지 않는다 |
| 모델은 제안만 | 스키마 검증·allow-list·상태 전이·승인·실행·기록은 전부 Core가 소유 |
| 판단과 능력의 분리 | "실행기가 없다"와 "이해할 수 없다"는 다른 이유로 기록한다 |
| 근거 있는 분류 | 근거가 부족하면 거절하지 않고 `정보 부족`과 보완 요청을 남긴다 |

자세한 계약은 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)에 있다.

## 아키텍처

```text
INPUT — 사용자·플랫폼별 수집 / 인증 / 연결 / 출처 근거
   │     Chrome Extension, Source Adapter (게시판·메일·메신저)
   ↓  출처와 업무 의미를 분리한 공통 WorkItem 경계
GENERIC Core
   ├─ S1~S3  정규화 · 적재 · 첨부 추출
   ├─ S4     규칙 기반 분류            ← 명확한 건은 여기서 끝
   ├─ S5     자동화 가능성 평가        ← 모호한 건만 외부 모델에 제안 요청
   │            ↓ structured proposal
   │         Core Validator (automation/hermes_validator.py)
   │            ↓
   ├─ S6~S7  Recipe allow-list → 사람 승인 → 준비 수행 → 검증
   └─ S8~S9  알림 · 이벤트 기록 · 대시보드
```

외부 모델과 Core의 권위는 다음과 같이 나뉜다.

| 담당 | 주체 |
|---|---|
| 모호한 분류·평가 **제안** | 외부 모델 (Skill/API) |
| 스키마 검증, Recipe allow-list, 상태 전이, 승인, 실행, 기록 | **Core** |

## 빠른 시작

의존성 설치가 필요 없다. Python 3.12 이상이면 된다.

```bash
git clone https://github.com/sn-owl/automation-webs.git
cd automation-webs

# 전체 테스트 (표준 라이브러리만 사용)
python3 -m unittest discover -s tests

# 공개 안전 게이트 — 업무 문서·자격증명이 섞이지 않았는지 검사
python3 scripts/check_public_safety.py
```

### 3분 데모 — 비식별 샘플 3건을 끝까지 돌려보기

```bash
python3 run_demo.py --output-root ./demo-out
```

세 시나리오(`ready` / `manual` / `developable`)가 각각 수집 → 분류 → 판정까지 진행되고,
결과와 근거가 `demo-out/` 아래 남는다. 세 건 모두 `review_required`로 끝나는 것이
정상이다 — **승인 전에는 아무것도 실행되지 않는다는 것이 이 제품의 핵심 동작**이다.

미리 만들어 둔 결과 예시는 [output-example/](output-example/)에서 바로 볼 수 있다.

### 승인 경계를 직접 확인하기

데모 결과는 시나리오별 디렉터리(`ready` / `manual` / `developable`)가 각각 하나의
runtime root다. `ready` 시나리오로 승인 게이트를 눌러 본다.

```bash
ROOT=./demo-out/ready

# 수집된 업무와 분류 근거 확인
python3 taskctl.py list --root $ROOT
python3 taskctl.py show alpha-13452 --root $ROOT

# ① 승인 없이 실행 시도 → 차단된다
python3 taskctl.py execute alpha-13452 --recipe restarea-hwpx-to-xls --root $ROOT
# taskctl failed: execute blocked: task has no recorded approval

# ② 사람이 승인한다 (누가, 왜 승인했는지가 함께 기록된다)
python3 taskctl.py approve alpha-13452 --actor "담당자" --reason "요청 확인함" --root $ROOT

# ③ 승인해도 등록되지 않은 Recipe는 실행되지 않는다
python3 taskctl.py execute alpha-13452 --recipe restarea-hwpx-to-xls --root $ROOT
# taskctl failed: unknown recipe id: restarea-hwpx-to-xls
```

승인과 Recipe 등록은 **서로 다른 두 개의 관문**이다. 하나를 통과해도 나머지가 막는다.
Recipe는 `create → test → review → activate` 단계를 거쳐야 실행 대상이 된다.

```bash
python3 recipectl.py --help        # 드래프트 작성 · 시험 · 검토 · 활성화
python3 taskctl.py --help          # 승인 · 보류 · 실행 · 재작성 · 완료 확인
```

### 대시보드 (선택)

```bash
pip install -r requirements-dashboard.txt
python3 start.py --runtime-root ./demo-out/ready
```

읽기 전용 대시보드에서 업무 상태, 분류 근거, 결과 version/attempt, 알림 전달 상태를 확인한다.

## 저장소 구조

```text
automation/            Core 패키지 — 분류·평가·패턴·Recipe·실행·검증·알림 (53개 모듈)
  ├─ adapters/         출처별 입력 어댑터 (그누보드, eGov 게시판)
  ├─ executors/        준비 수행 실행기 (excel, restarea, code_analysis)
  └─ files/            첨부 파일 핸들러 (hwpx, passthrough)
config/                규칙·승인·평가·Recipe 정의 (코드 수정 없이 바꾸는 데이터)
schemas/               WorkItem·분류·평가·Recipe·결정의 JSON Schema
tests/                 60개 테스트 파일 / 697개 테스트
extension/             Chrome Extension — 웹 게시판 업무 수집기 (S1 입력 계층)
hermes/                외부 모델 연동 프롬프트와 파이프라인 스크립트
skills/                모델에게 주는 구조화된 제안 계약 (Skill 정의)
restarea-converter/    HWPX → XLS 변환기 (Executor 구현 예시, Windows 전용)
fixtures/sanitized/    비식별 게시판 HTML 샘플 + 비식별 정책
output-example/        세 시나리오의 실제 산출물 예시
scripts/               공개 안전 게이트, 비식별 도구, 스케줄러 등록 스크립트
docs/ARCHITECTURE.md   설계 계약 전문
```

주요 실행 파일:

| 파일 | 역할 |
|---|---|
| `run_demo.py` | 비식별 샘플 3건 오프라인 데모 |
| `run_pipeline.py` | 수집 → 분류 → 평가 파이프라인 |
| `taskctl.py` | 업무 조회·승인·실행·확인 CLI |
| `recipectl.py` | Recipe 등록·조회 CLI |
| `inbox_runner.py` | 수집 번들 감시·처리 루프 |
| `dashboard.py` | Streamlit 읽기 전용 대시보드 |
| `start.py` | 대시보드 + 러너 통합 실행 |
| `normalize.py` | 게시판 HTML 한 건 → WorkItem JSON 변환 |
| `parser.py` | HWPX 표준 양식 파서 (`--selftest`는 로컬 문서가 있어야 동작) |

## 테스트

```bash
python3 -m unittest discover -s tests    # Core 697건
cd extension && node --test               # Extension 5건
python3 scripts/check_public_safety.py    # 공개 안전 게이트 (0 findings 여야 함)
```

Windows에서는 `run_tests.bat`으로 같은 테스트를 실행한다.

첨부 이미지 미리보기 테스트는 Pillow가 있을 때만 돈다.

```bash
pip install -r requirements-dev.txt       # Pillow — 없으면 해당 테스트는 skip
```

## 보안·개인정보 경계

이 저장소는 **실제 업무 문서를 포함하지 않는다.**

- 공개 fixture는 전부 치환 placeholder(`PERSON_001`, `DEPARTMENT_001`, `example.invalid`)를 쓴다 — [fixtures/sanitized/POLICY.md](fixtures/sanitized/POLICY.md)
- 실명·연락처·시설 주소가 있는 원본, 치환표, 자격증명은 `.gitignore`로 차단한다
- `scripts/check_public_safety.py`가 업무 문서·자격증명 유입을 검사하고, 테스트가 **0 findings**를 강제한다
- 외부 채널(메신저 알림 등)은 명시적 설정과 승인이 있을 때만 활성화된다

사이트마다 "공개 도메인이지만 실제로는 내부용"인 호스트가 있다. 그 목록을 소스에
적으면 지우려는 것을 그대로 공개하게 되므로, 환경변수로 받는다.

```bash
export SANITIZE_PRIVATE_HOSTS="boards.example.kr,intranet.example.kr"
```

`.local` 이름과 사설 IP 대역(RFC1918)은 설정 없이도 항상 차단한다.

## 한계

정직하게 적어 둔다.

- 테스트는 합성 입력과 로컬 결정적 어댑터를 사용한다
- 실제 로그인 세션 수집, Windows Excel COM 실행, 외부 모델·메신저 발송은 각 환경 설정이 있어야 동작하며 자동 테스트 범위 밖이다
- `restarea-converter`는 Windows + 한글/Excel 환경 전용이다
- 기존 파일 수정과 외부 시스템 반영은 **의도적으로** 사람의 몫으로 남겨 두었다

## 기여

[CONTRIBUTING.md](CONTRIBUTING.md)를 참고한다. 이슈와 PR을 환영한다.

## 라이선스

[Apache License 2.0](LICENSE). 자세한 저작권 고지는 [NOTICE](NOTICE)를 참고한다.
