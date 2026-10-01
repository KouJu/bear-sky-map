import { defineConfig } from "vite";

// GitHub Pages serves a project repo under /<repo-name>/, so built asset and
// data URLs need that prefix. Dev keeps "/" so localhost is unaffected.
// If the repository is renamed, change this to match.
export default defineConfig({
  base: process.env.GITHUB_ACTIONS ? "/bear-sky-map/" : "/",
});
