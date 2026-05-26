declare module "node:assert/strict" {
  const assert: {
    deepEqual(actual: unknown, expected: unknown, message?: string): void;
    doesNotMatch(actual: string, regexp: RegExp, message?: string): void;
    equal(actual: unknown, expected: unknown, message?: string): void;
    match(actual: string, regexp: RegExp, message?: string): void;
    notEqual(actual: unknown, expected: unknown, message?: string): void;
    throws(block: () => unknown, error?: (err: unknown) => boolean): void;
  };
  export default assert;
}

declare module "node:fs/promises" {
  export function readdir(path: string): Promise<string[]>;
  export function readFile(path: string, encoding: "utf8"): Promise<string>;
}

declare module "node:test" {
  export default function test(
    name: string,
    fn: () => void | Promise<void>,
  ): void;
}
