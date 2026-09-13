# 기여 가이드

## 시작하기

```bash
git clone <저장소 URL>
cd automation_webs_public
python3 -m unittest discover -s tests
```

Core는 Python 표준 라이브러리만 사용한다. 새 기능을 위해 서드파티 의존성을 추가하려면
**왜 표준 라이브러리로 안 되는지**를 PR 설명에 적어 주기 바란다.

## 반드시 지켜야 하는 경계

이 프로젝트는 실제 업무 문서와 승인 절차를 다룬다. 아래는 협상 대상이 아니다.

1. **실제 업무 문서를 커밋하지 않는다.** 게시판 HTML, 첨부 파일, 실명·연락처·주소가 들어간
   어떤 파일도 올리지 않는다. fixture가 필요하면 `scripts/sanitize_fixture.py`로 비식별한 뒤
   [fixtures/sanitized/POLICY.md](fixtures/sanitized/POLICY.md)를 따른다.
2. **자격증명을 커밋하지 않는다.** 토큰·비밀번호·세션 쿠키는 `.env`나 OS 설정에 둔다.
3. **승인 경계를 우회하지 않는다.** 등록되지 않은 Recipe를 실행하거나, 사람 승인 없이
   실행 경로를 여는 변경은 받지 않는다.
4. **원본을 수정하지 않는다.** 산출물은 언제나 새 파일이어야 한다.

PR 전에 반드시 통과해야 하는 검사:

```bash
python3 -m unittest discover -s tests
python3 scripts/check_public_safety.py   # 0 findings
cd extension && node --test
```

`check_public_safety.py`가 findings를 보고하면 CI가 실패한다. 이것은 경고가 아니라 게이트다.

## 테스트

- 모든 동작 변경에는 테스트가 따라야 한다
- 테스트 이름은 "무엇이 보장되는가"를 문장으로 쓴다
  (예: `test_stale_claim_file_does_not_block_retry`)
- 임시 디렉터리를 쓸 때는 `Path(directory).resolve()`로 감싼다 — macOS에서 `/var`가
  `/private/var`의 심볼릭 링크라 resolve 하지 않으면 경로 비교가 깨진다
- 선택 의존성이 필요한 테스트는 `unittest.skipUnless`로 건너뛸 수 있게 한다

## 커밋과 PR

- 커밋 메시지는 **무엇을 왜** 바꿨는지 한 줄로 요약한다
- 하나의 PR은 하나의 관심사만 다룬다
- 설계 계약을 바꾸는 변경이라면 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)도 함께 갱신한다

## 이슈

버그 리포트에는 재현 절차, 기대 동작, 실제 동작을 적어 주기 바란다.
**실제 업무 문서나 스크린샷의 개인정보는 가리고** 올려 주기 바란다.
