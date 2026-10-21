/*
 * ============================================================================
 * extension.test.ts — 扩展测试文件
 * ============================================================================
 *
 * 测试 airi-monitor 扩展的核心逻辑单元。
 */

import * as assert from 'assert';
import * as vscode from 'vscode';
import { supportedLanguageIds } from '../extension';

suite('Extension Test Suite', () => {
	vscode.window.showInformationMessage('Start all tests.');

	/**
	 * 验证扩展已正确激活并注册了命令
	 */
	test('Extension should be activated', async () => {
		const ext = vscode.extensions.getExtension('123457890wasd-cmyk.vscode-anime-assistent');
		assert.ok(ext, 'Extension not found');

		if (!ext.isActive) {
			await ext.activate();
		}
		assert.strictEqual(ext.isActive, true, 'Extension should be active');
	});

	/**
	 * 验证 openAssistant 命令已注册
	 */
	test('openAssistant command should be registered', async () => {
		const commands = await vscode.commands.getCommands(true);
		assert.strictEqual(
			commands.includes('vscode-anime-assistent.openAssistant'),
			true,
			'openAssistant command not registered'
		);
	});

	/**
	 * 验证 launchPet 命令已注册
	 */
	test('launchPet command should be registered', async () => {
		const commands = await vscode.commands.getCommands(true);
		assert.strictEqual(
			commands.includes('vscode-anime-assistent.launchPet'),
			true,
			'launchPet command not registered'
		);
	});

	/**
	 * 验证支持的语言 ID 集合。
	 *
	 * ⚠️ 0.3.41 —— 以前这里是在测试文件里另抄一份 `['c','cpp','python']`
	 * 然后断言它包含 c/cpp/python：把字面量改成 ['rust','go','ts'] 依旧
	 * 全绿，测不到任何产品代码。现在直接断言被测模块的真实集合，并且
	 * **反向**断言（javascript 不在其中）也有了意义 —— 它防的是
	 * `supportedLanguageIds` 被误改成"全语言通吃"。
	 */
	test('should support C, C++, and Python language IDs', () => {
		assert.strictEqual(supportedLanguageIds.has('c'), true, 'c not supported');
		assert.strictEqual(supportedLanguageIds.has('cpp'), true, 'cpp not supported');
		assert.strictEqual(supportedLanguageIds.has('python'), true, 'python not supported');
		assert.strictEqual(supportedLanguageIds.has('javascript'), false,
			'javascript should not be supported');
		assert.strictEqual(supportedLanguageIds.size, 3,
			`unexpected set: ${[...supportedLanguageIds].join(',')}`);
	});
});
