/* File-manager path handling.
 *
 * A customer reported the file browser showing "//home/dev". The root crumb's
 * label is "/" and every crumb was ALSO joined with a "/" separator, so the
 * root's own text and the first separator were the same character. */
import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const { crumbs, crumbsText, join, normalize } = await import(
  path.join(ROOT, "web/js/paths.js"));

// ---- the reported bug ----------------------------------------------------
test("the home directory renders with ONE leading slash", () => {
  assert.equal(crumbsText("/home/dev"), "/home/dev");
});

test("no path renders a doubled slash", () => {
  for (const p of ["/", "/home", "/home/dev", "/home/dev/projects/app",
                   "/a/b/c/d/e", "/tmp"]) {
    assert.ok(!crumbsText(p).includes("//"), `${p} -> ${crumbsText(p)}`);
  }
});

test("the trail reads back as the path it describes", () => {
  for (const p of ["/", "/home", "/home/dev", "/var/lib/docker", "/a/b/c"]) {
    assert.equal(crumbsText(p), p);
  }
});

test("the root is just a slash", () => {
  assert.equal(crumbsText("/"), "/");
});

// ---- every segment stays clickable ---------------------------------------
test("each segment links to its own absolute path", () => {
  const html = crumbs("/home/dev/projects");
  for (const target of ["/", "/home", "/home/dev", "/home/dev/projects"]) {
    assert.ok(html.includes(`data-go="${target}"`), `missing crumb for ${target}`);
  }
});

test("a segment never links to a doubled path", () => {
  assert.ok(!crumbs("/home/dev").includes('data-go="//'));
});

// ---- names that are not tidy ---------------------------------------------
test("odd input does not produce a broken trail", () => {
  assert.equal(crumbsText(""), "/");
  assert.equal(crumbsText("//home//dev//"), "/home/dev");
  assert.equal(crumbsText("/home/dev/"), "/home/dev");
  assert.equal(crumbsText(undefined), "/");
});

test("a directory name containing markup is escaped", () => {
  const html = crumbs('/home/<script>alert(1)</script>');
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("&lt;script&gt;"));
});

// ---- join, which had the same class of bug -------------------------------
test("join never doubles a slash", () => {
  assert.equal(join("/", "notes.txt"), "/notes.txt");
  assert.equal(join("/home/dev", "notes.txt"), "/home/dev/notes.txt");
  // The server's reported path may carry a trailing slash.
  assert.equal(join("/home/dev/", "notes.txt"), "/home/dev/notes.txt");
  assert.equal(join("/home/dev", "/notes.txt"), "/home/dev/notes.txt");
  assert.equal(join("/", "/notes.txt"), "/notes.txt");
});

test("normalize is idempotent", () => {
  for (const p of ["/", "/home", "/home/dev/", "//home//dev"]) {
    assert.equal(normalize(normalize(p)), normalize(p));
  }
});
