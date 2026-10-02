import unittest

from account_statistics import ACCOUNT_COLUMNS, ACCOUNT_FIELD, encode_accounts, account_row


class AccountStatisticsTests(unittest.TestCase):
    def test_export_uses_each_accounts_own_statistics(self):
        accounts = [{name: float(i) for name in ACCOUNT_COLUMNS} for i in (74272, 156)]
        row = {"交易次数（单）": "74272", ACCOUNT_FIELD: encode_accounts(accounts)}
        self.assertEqual(account_row(row, 0)["交易次数（单）"], "74272")
        self.assertEqual(account_row(row, 1)["交易次数（单）"], "156")
        self.assertEqual(row["交易次数（单）"], "74272")

    def test_incomplete_statistics_are_not_silently_accepted(self):
        with self.assertRaises(ValueError):
            account_row({ACCOUNT_FIELD: "1|2"}, 0)

    def test_v127_has_no_actual_capacity_and_is_still_readable(self):
        row = account_row({ACCOUNT_FIELD: "|".join(["1"] * 12)}, 0)
        self.assertEqual(row["交易次数（单）"], "1")
        self.assertNotIn("实际容量占用率（%）", row)
