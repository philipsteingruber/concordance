import unittest
import unittest.mock

from concordance.config import ConfigError, env_number


class EnvNumberTest(unittest.TestCase):
    def test_uses_the_default_when_the_variable_is_unset(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(env_number("CONCORDANCE_X", 5000, int), 5000)

    def test_reads_a_number_from_the_environment(self):
        with unittest.mock.patch.dict("os.environ", {"CONCORDANCE_X": "7500"}):
            self.assertEqual(env_number("CONCORDANCE_X", 5000, int), 7500)

    def test_names_the_variable_when_its_value_is_not_a_number(self):
        with unittest.mock.patch.dict("os.environ", {"CONCORDANCE_X": "5GB"}):
            with self.assertRaisesRegex(ConfigError, "CONCORDANCE_X"):
                env_number("CONCORDANCE_X", 5000, int)


if __name__ == "__main__":
    unittest.main()
