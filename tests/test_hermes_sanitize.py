import unittest


class HermesSanitizeTest(unittest.TestCase):
    def test_keeps_work_context_and_masks_sensitive_values(self):
        from automation.hermes_sanitize import mask_for_hermes

        masked = mask_for_hermes(
            "작업구분: 수정\n"
            "담당자: 홍길동\n"
            "전화번호: 010-1234-5678\n"
            "문의: owner@example.com\n"
            "URL: https://internal.example/post?wr_id=12\n"
            "password=secret-value\n"
            "본문의 핵심 작업 내용"
        )

        self.assertIn("작업구분: 수정", masked)
        self.assertIn("본문의 핵심 작업 내용", masked)
        for forbidden in (
            "홍길동",
            "010-1234-5678",
            "owner@example.com",
            "internal.example",
            "secret-value",
        ):
            self.assertNotIn(forbidden, masked)

    def test_body_is_bounded(self):
        from automation.hermes_sanitize import MAX_HERMES_BODY_CHARS, mask_for_hermes

        masked = mask_for_hermes("가" * (MAX_HERMES_BODY_CHARS + 100))

        self.assertLessEqual(len(masked), MAX_HERMES_BODY_CHARS + 20)
        self.assertTrue(masked.endswith("[본문 일부 생략]"))


if __name__ == "__main__":
    unittest.main()
