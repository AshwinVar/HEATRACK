import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

/// Dart side of the Kotlin Health Connect collector (Android only). On other platforms
/// [isSupported] is false and the wearer collector UI explains why.
class CollectorBridge {
  static const _ch = MethodChannel('familypulse/collector');

  static bool get isSupported =>
      !kIsWeb && defaultTargetPlatform == TargetPlatform.android;

  static Future<String?> launchAction() =>
      _ch.invokeMethod<String>('launchAction');
  static Future<String> installationId() async =>
      (await _ch.invokeMethod<String>('installationId'))!;
  static Future<String> healthConnectStatus() async =>
      (await _ch.invokeMethod<String>('healthConnectStatus'))!;

  static Future<Map<String, dynamic>> status() async =>
      jsonDecode((await _ch.invokeMethod<String>('status'))!)
          as Map<String, dynamic>;

  static Future<List<String>> requestPermissions({
    required bool includeBackground,
  }) async {
    final r = await _ch.invokeMethod<List<dynamic>>('requestPermissions', {
      'includeBackground': includeBackground,
    });
    return (r ?? const []).cast<String>();
  }

  static Future<bool> openHealthConnect() async =>
      (await _ch.invokeMethod<bool>('openHealthConnect')) ?? false;

  static Future<void> configure({
    required String apiBaseUrl,
    required String profileId,
    required String deviceId,
  }) => _ch.invokeMethod('configure', {
    'apiBaseUrl': apiBaseUrl,
    'profileId': profileId,
    'deviceId': deviceId,
  });

  static Future<void> setSession(Map<String, dynamic>? session) =>
      _ch.invokeMethod('setSession', {
        'session': session == null ? null : jsonEncode(session),
      });

  /// Kotlin owns refresh on Android (single refresher; see SessionStore.kt).
  static Future<String> accessToken() async =>
      (await _ch.invokeMethod<String>('accessToken'))!;

  static Future<void> enable() => _ch.invokeMethod('enable');
  static Future<void> disable() => _ch.invokeMethod('disable');
  static Future<void> signOut() => _ch.invokeMethod('signOut');

  static Future<Map<String, dynamic>> syncNow() async =>
      jsonDecode((await _ch.invokeMethod<String>('syncNow'))!)
          as Map<String, dynamic>;

  static Future<Map<String, dynamic>> diagnose({int hours = 24}) async =>
      jsonDecode(
        (await _ch.invokeMethod<String>('diagnose', {'hours': hours}))!,
      ) as Map<String, dynamic>;
}
