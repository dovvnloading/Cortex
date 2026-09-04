import eslint from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "coverage", "test-results", "playwright-report"] },
  eslint.configs.recommended,
  tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: "latest",
      // Application source ships to a browser. Node and Vitest globals were
      // declared here too, which made `process`, `require`, `__dirname` and
      // `describe` look defined inside components -- so a Node-only API
      // leaking into the bundle read as ordinary code.
      globals: globals.browser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.flat.recommended.rules,
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
  {
    // Tests and tooling legitimately run under Node and Vitest.
    files: [
      "**/*.{test,spec}.{ts,tsx}",
      "src/test/**/*.{ts,tsx}",
      "e2e/**/*.{ts,tsx}",
      "*.config.{ts,js}",
    ],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node, ...globals.vitest },
    },
  },
);
