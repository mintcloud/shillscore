import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: "#0b0b0f",
        surface: "#13131a",
        ink: "#e6e6ea",
        muted: "#9aa0a6",
        accent: "#7dd3fc",
      },
      fontFamily: {
        sans: ["Geist", "Inter", "system-ui", "sans-serif"],
        mono: ["GeistMono", "ui-monospace", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
