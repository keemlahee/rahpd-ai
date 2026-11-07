# 현장노트 구조와 템플릿 범위

## 대상

주거·상가 설비 및 수도 배관 시공을 하는 1인 사업자용 반응형 웹앱. 기존 온라인 주소와 사용자의 Google Cloud·토스 설정은 새 앱에 복사하지 않는다. 기존 서비스를 수정할 때는 사용자가 지정한 체크아웃이나 Sites의 소스 저장소와 현재 배포를 확인한다.

## 소스 지도

| 위치 | 역할 |
| --- | --- |
| `public/app.js`, `public/styles.css`, `public/domain.js` | 현장·견적·기록·설정·구독 화면, 디자인, 계산 |
| `worker.mjs` | 온라인 API, D1/R2 소유권, 구독 접근 제한, Google 콜백 |
| `google-auth.mjs` | Google ID 토큰 검증, 보안 쿠키 세션 |
| `billing-core.mjs` | 월 요금·이용기간·토스 API·빌링키 암호화 |
| `server.mjs` | 로컬 Node/SQLite 실행. 온라인 인증·데이터와 별개 |
| `db/schema.ts`, `drizzle/` | 문서, 파일, 구독, 주문, 로그인 세션 구조와 마이그레이션 |
| `test/app.test.mjs` | 저장, 소유권, 구독, Google ID 토큰의 자동 검증 |
| `build.mjs`, `package-site.mjs` | Sites Worker 산출물과 배포 압축 파일 |

## 새 앱으로 복사할 때

스타터에는 위 소스와 테스트, 패키지 설정만 있다. `.openai/hosting.json`, `.git`, `dist/`, `data/`, `node_modules/`, 기존 README, API 키와 실제 현장 데이터는 없다. 로컬 실행은 Node.js 24 이상에서 `node server.mjs`다. 온라인 배포 전에는 새 Sites 프로젝트의 **실제 반환된** `project_id`와 D1 `DB`, R2 `BUCKET` 바인딩으로 `.openai/hosting.json`을 준비한다. `build.mjs`는 이 파일을 요구한다.

Google Cloud OAuth 웹 클라이언트에는 새 사이트의 HTTPS 원본과 `<새 사이트 주소>/api/auth/google` 리디렉션 URI가 필요하다. `GOOGLE_CLIENT_ID`는 공개 가능한 클라이언트 ID지만 Sites 환경 변수로 분리한다. `GOOGLE_OWNER_EMAIL` 및 `GOOGLE_OWNER_USER_ID`는 기존 사용자 데이터를 이관할 때만 사용한다. OAuth 앱이 테스트 중이면 허용 테스트 사용자만 로그인할 수 있다.

토스 테스트 구독에 쓰는 환경 변수는 `TOSS_CLIENT_KEY`, `TOSS_SECRET_KEY`, `BILLING_ENCRYPTION_KEY`다. 뒤의 두 값은 비밀이다. 기존 암호화 키를 바꾸면 저장된 빌링키를 읽지 못하므로, 운영 중에는 마이그레이션 없이 교체하지 않는다. 현재 템플릿의 `scheduled` 처리기는 호스팅 일정이 연결되지 않으면 실행되지 않고, 만료 후 재접속 시 갱신만 시도한다. 토스 카드 등록과 승인에 관한 자동 테스트는 모의 응답이며 실제 카드 승인 검증을 대신하지 않는다.

예시 화면의 견적 단가는 실거래 단가가 아니다. 사진·녹음 첨부와 브라우저 한국어 받아쓰기를 제공하지만 자동 AI 전사는 없다.
