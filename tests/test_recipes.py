import dataclasses
import json
import unittest
from pathlib import Path


SCHEMA_PATH = Path(__file__).parents[1] / "schemas" / "recipe.schema.json"
SHIPPED_RECIPE_PATH = Path(__file__).parents[1] / "config" / "recipes" / "restarea-hwpx-to-xls.json"

RESTAREA = {
    "recipe_id": "restarea-hwpx-to-xls",
    "description": "무더위쉼터 현행화 신청서 HWPX를 고정 XLS 업로드 양식으로 변환한다",
    "executor": "restarea-converter",
    "input_type": "hwpx",
    "output_type": "xls",
    "scope": "legacy",
    "template": "restarea-converter/restarea-template.xls",
}

NO_TEMPLATE = {
    "recipe_id": "no-template-recipe",
    "description": "템플릿이 없는 변환",
    "executor": "restarea-converter",
    "input_type": "pdf",
    "output_type": "pdf",
    "scope": "legacy",
}


class RecipeContractTest(unittest.TestCase):
    def model(self):
        try:
            from automation.recipes import RecipeDefinition
        except ModuleNotFoundError as exc:
            self.fail(f"automation.recipes should exist: {exc}")
        return RecipeDefinition

    def schema(self):
        try:
            return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            self.fail(f"recipe schema should exist: {exc}")

    # -- RecipeDefinition contract -----------------------------------

    def test_round_trips_to_canonical_dict(self):
        RecipeDefinition = self.model()
        result = RecipeDefinition.from_dict(RESTAREA)

        self.assertEqual(result.to_dict(), RESTAREA)
        self.assertEqual(
            json.loads(json.dumps(result.to_dict(), ensure_ascii=False)), RESTAREA
        )

    def test_recipe_without_template_round_trips_without_key(self):
        RecipeDefinition = self.model()
        result = RecipeDefinition.from_dict(NO_TEMPLATE)

        self.assertIsNone(result.template)
        self.assertNotIn("template", result.to_dict())
        self.assertEqual(result.to_dict(), NO_TEMPLATE)

    def test_model_is_frozen(self):
        RecipeDefinition = self.model()
        result = RecipeDefinition.from_dict(RESTAREA)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.executor = "something-else"

    def test_missing_field_is_rejected(self):
        RecipeDefinition = self.model()
        for field in ("recipe_id", "description", "executor", "input_type", "output_type"):
            payload = {k: v for k, v in RESTAREA.items() if k != field}
            with self.subTest(missing=field):
                with self.assertRaisesRegex(ValueError, field):
                    RecipeDefinition.from_dict(payload)

    def test_blank_recipe_id_is_rejected(self):
        RecipeDefinition = self.model()
        with self.assertRaisesRegex(ValueError, "recipe_id"):
            RecipeDefinition.from_dict(dict(RESTAREA, recipe_id="  "))

    def test_blank_description_is_rejected(self):
        RecipeDefinition = self.model()
        with self.assertRaisesRegex(ValueError, "description"):
            RecipeDefinition.from_dict(dict(RESTAREA, description=""))

    def test_input_type_outside_enum_is_rejected(self):
        RecipeDefinition = self.model()
        with self.assertRaisesRegex(ValueError, "input_type"):
            RecipeDefinition.from_dict(dict(RESTAREA, input_type="exe"))

    def test_output_type_outside_enum_is_rejected(self):
        RecipeDefinition = self.model()
        with self.assertRaisesRegex(ValueError, "output_type"):
            RecipeDefinition.from_dict(dict(RESTAREA, output_type="exe"))

    def test_unknown_field_is_rejected(self):
        RecipeDefinition = self.model()
        with self.assertRaisesRegex(ValueError, "command"):
            RecipeDefinition.from_dict(dict(RESTAREA, command="rm -rf /"))

    # -- Executor is an allow-listed key, never a command -------------

    def test_executor_outside_allowlist_is_rejected(self):
        RecipeDefinition = self.model()
        with self.assertRaisesRegex(ValueError, "executor"):
            RecipeDefinition.from_dict(dict(RESTAREA, executor="rm -rf /"))

    def test_known_executors_hold_no_command_or_path_surface(self):
        from automation.recipes import EXECUTORS

        self.assertIn("restarea-converter", EXECUTORS)
        for value in EXECUTORS.values():
            self.assertIsInstance(value, str)
            self.assertNotIn(";", value)
            self.assertNotIn("|", value)

    # -- Step 4: path-valued fields must stay inside the repository ---

    def test_template_dotdot_escape_is_rejected(self):
        RecipeDefinition = self.model()
        with self.assertRaisesRegex(ValueError, "template"):
            RecipeDefinition.from_dict(dict(RESTAREA, template="../../etc/passwd"))

    def test_template_dotdot_escape_in_middle_is_rejected(self):
        RecipeDefinition = self.model()
        with self.assertRaisesRegex(ValueError, "template"):
            RecipeDefinition.from_dict(
                dict(RESTAREA, template="restarea-converter/../../outside.xls")
            )

    def test_template_absolute_unix_path_is_rejected(self):
        RecipeDefinition = self.model()
        with self.assertRaisesRegex(ValueError, "template"):
            RecipeDefinition.from_dict(dict(RESTAREA, template="/etc/passwd"))

    def test_template_drive_letter_jump_is_rejected(self):
        RecipeDefinition = self.model()
        with self.assertRaisesRegex(ValueError, "template"):
            RecipeDefinition.from_dict(
                dict(RESTAREA, template=r"C:\Windows\System32\config.xls")
            )

    def test_template_unc_path_is_rejected(self):
        RecipeDefinition = self.model()
        with self.assertRaisesRegex(ValueError, "template"):
            RecipeDefinition.from_dict(
                dict(RESTAREA, template=r"\\attacker-host\share\payload.xls")
            )

    def test_template_relative_inside_repo_is_accepted(self):
        RecipeDefinition = self.model()
        result = RecipeDefinition.from_dict(RESTAREA)
        self.assertEqual(result.template, "restarea-converter/restarea-template.xls")

    # -- schema mirrors the model contract -----------------------------

    def test_schema_matches_model_contract(self):
        from automation.recipes import EXECUTORS, RecipeDefinition

        schema = self.schema()

        self.assertEqual(
            set(schema["required"]),
            {"recipe_id", "description", "executor", "input_type", "output_type", "scope"},
        )
        self.assertEqual(set(schema["properties"]), set(RESTAREA))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]["executor"]["enum"]), set(EXECUTORS))
        self.assertEqual(
            set(schema["properties"]["input_type"]["enum"]),
            set(RecipeDefinition.INPUT_OUTPUT_TYPES),
        )
        self.assertEqual(
            set(schema["properties"]["output_type"]["enum"]),
            set(RecipeDefinition.INPUT_OUTPUT_TYPES),
        )

    def test_schema_validates_sample_and_rejects_bad_enum(self):
        jsonschema = _load_jsonschema()
        if jsonschema is None:
            self.skipTest("jsonschema not installed")
        schema = self.schema()

        jsonschema.validate(RESTAREA, schema)
        jsonschema.validate(NO_TEMPLATE, schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(dict(RESTAREA, input_type="exe"), schema)


class RecipeRegistryTest(unittest.TestCase):
    def registry(self):
        try:
            from automation.recipes import UnknownRecipeError, get_recipe, load_recipes
        except ModuleNotFoundError as exc:
            self.fail(f"automation.recipes should exist: {exc}")
        return UnknownRecipeError, get_recipe, load_recipes

    def test_known_recipe_lookup_returns_definition(self):
        _, get_recipe, _ = self.registry()

        result = get_recipe("restarea-hwpx-to-xls", recipes={"restarea-hwpx-to-xls": RESTAREA})

        self.assertEqual(result.recipe_id, "restarea-hwpx-to-xls")
        self.assertEqual(result.executor, "restarea-converter")

    def test_arbitrary_id_is_rejected(self):
        UnknownRecipeError, get_recipe, _ = self.registry()

        with self.assertRaisesRegex(UnknownRecipeError, "unknown"):
            get_recipe("does-not-exist", recipes={"restarea-hwpx-to-xls": RESTAREA})

    def test_load_recipes_rejects_recipe_id_mismatched_with_key(self):
        _, _, load_recipes = self.registry()

        with self.assertRaisesRegex(ValueError, "mismatch"):
            load_recipes(recipes={"wrong-key": RESTAREA})

    def test_load_recipes_returns_dict_of_recipe_definitions(self):
        from automation.recipes import RecipeDefinition

        _, _, load_recipes = self.registry()
        registry = load_recipes(recipes={"restarea-hwpx-to-xls": RESTAREA})

        self.assertIsInstance(registry, dict)
        self.assertIsInstance(registry["restarea-hwpx-to-xls"], RecipeDefinition)

    # -- The shipped config/recipes/restarea-hwpx-to-xls.json, not a copy --

    def test_shipped_restarea_recipe_file_exists(self):
        self.assertTrue(
            SHIPPED_RECIPE_PATH.is_file(),
            f"expected shipped recipe at {SHIPPED_RECIPE_PATH}",
        )

    def test_shipped_restarea_recipe_loads_by_id(self):
        _, get_recipe, _ = self.registry()

        result = get_recipe("restarea-hwpx-to-xls")

        self.assertEqual(result.recipe_id, "restarea-hwpx-to-xls")
        self.assertEqual(result.executor, "restarea-converter")
        self.assertEqual(result.input_type, "hwpx")
        self.assertEqual(result.output_type, "xls")
        self.assertEqual(result.template, "restarea-converter/restarea-template.xls")

    def test_shipped_restarea_recipe_template_points_at_real_file(self):
        _, get_recipe, _ = self.registry()
        repo_root = Path(__file__).parents[1]

        result = get_recipe("restarea-hwpx-to-xls")

        self.assertTrue((repo_root / result.template).is_file())

    def test_shipped_unknown_recipe_id_is_still_rejected(self):
        UnknownRecipeError, get_recipe, _ = self.registry()

        with self.assertRaisesRegex(UnknownRecipeError, "unknown"):
            get_recipe("does-not-exist")


def _load_jsonschema():
    try:
        import jsonschema

        return jsonschema
    except ModuleNotFoundError:
        return None


if __name__ == "__main__":
    unittest.main()
