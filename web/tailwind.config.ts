import type { Config } from "tailwindcss";

export default {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0b0e14",
        panel: "#121722",
        line: "#232b3a",
        mut: "#8a94a8",
        pos: "#3ecf8e",
        neg: "#f2645a",
        acc: "#7aa2ff",
      },
      fontFamily: { mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"] },
    },
  },
  plugins: [],
} satisfies Config;
