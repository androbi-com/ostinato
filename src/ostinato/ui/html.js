// Preact's JSX-like templates without a build step: html`<${Component} prop=${value}>...<//>`
import { h } from "preact";
import htm from "htm";

export const html = htm.bind(h);
