import { readFile, realpath, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { isAbsolute, join, relative, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const [output, options, snapshot, expectedVitest] = process.argv.slice(2);
const selected = JSON.parse(await readFile(options, 'utf8'));
const root = process.cwd();
const load = createRequire(join(root, 'package.json'));
const record = { schema_version: 2, files: [], errors: [], vitest_version: null };
const errorRecord = (error) => ({
  type: String(error?.name ?? 'Error').slice(0, 200),
  message: String(error?.message ?? error).slice(0, 2000),
});
const relativePath = (file) => relative(snapshot, file).split(sep).join('/');
let vitest;
try {
  const installedVitest = await realpath(load.resolve('vitest/package.json'));
  const installedPath = relative(snapshot, installedVitest);
  if (isAbsolute(installedPath) || installedPath.split(sep)[0] === '..' ||
      installedVitest !== await realpath(expectedVitest)) {
    throw new Error('实际解析到的 Vitest 与锁文件记录的位置不同。');
  }
  record.vitest_version = load(installedVitest).version;
  const { createVitest } = await import(pathToFileURL(load.resolve('vitest/node')).href);
  vitest = await createVitest('test', {
    root,
    watch: false,
    reporters: [{}],
    coverage: { enabled: false },
    allowOnly: false,
    passWithNoTests: false,
    update: false,
    ui: false,
    api: false,
  });
  const discovered = await vitest.globTestSpecifications();
  if (selected === null) {
    if (discovered.some((specification) => specification.project.config.includeSource?.length)) {
      throw new Error('Vitest 的 includeSource 可能将实现文件纳入共享范围，请手动选择独立测试文件。');
    }
    record.files = discovered.map((specification) => relativePath(specification.moduleId));
  } else {
    await runSelected(vitest, discovered);
  }
} catch (error) {
  console.error(error);
  record.errors.push(errorRecord(error));
} finally {
  await vitest?.close();
}
await writeFile(output, JSON.stringify(record), { flag: 'wx' });
// Test failures are represented in the report. The worker exit describes transport.
process.exitCode = 0;

async function runSelected(vitest, discovered) {
  const wanted = new Set(selected.map((file) => resolve(snapshot, file)));
  const specifications = discovered
    .filter((specification) => wanted.has(resolve(specification.moduleId)));
  if (specifications.length !== wanted.size ||
      new Set(specifications.map((specification) => resolve(specification.moduleId))).size !== wanted.size) {
    throw new Error('Vitest 配置中找不到部分所选文件，或同一文件属于多个测试配置。请核对 include、exclude 和 projects。');
  }
  const result = await vitest.runTestSpecifications(specifications);
  record.errors = result.unhandledErrors.map(errorRecord);
  for (const module of result.testModules) {
    const errors = module.errors().map(errorRecord);
    for (const suite of module.children.allSuites()) {
      errors.push(...suite.errors().map(errorRecord));
    }
    const tests = Array.from(module.children.allTests(), (test) => {
      const result = test.result();
      const diagnostic = test.diagnostic();
      return {
        name: test.fullName,
        status: result.state,
        errors: (result.errors ?? []).map(errorRecord),
        expected_failure: test.options.fails === true,
        retry_count: diagnostic?.retryCount ?? null,
        repeat_count: diagnostic?.repeatCount ?? null,
        flaky: diagnostic?.flaky === true,
      };
    });
    record.files.push({ path: relativePath(module.moduleId), project: module.project.name, errors, tests });
  }
}
