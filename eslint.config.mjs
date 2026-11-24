import js from "@eslint/js";
import globals from "globals";

export default [
  { ignores: ["node_modules/**", ".venv/**", "dist/**", "tests/fixtures/**"] },
  {
    ...js.configs.recommended,
    files: ["narratarr/static/**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: globals.browser,
    },
  },
  {
    ...js.configs.recommended,
    files: ["*.mjs"],
    languageOptions: { globals: globals.node },
  },
];
