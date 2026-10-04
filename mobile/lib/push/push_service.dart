import 'dart:async';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import '../api/api_client.dart';

/// FCM registration, foreground presentation and alert deep links.
///
/// Firebase is optional at build time. Without google-services.json (Android) /
/// GoogleService-Info.plist + APNs key (iOS) initialisation fails and [state] explains why;
/// alerts are still visible in the app. Web push is not implemented (web is a fallback
/// dashboard only).
class PushService extends ChangeNotifier {
  PushService(this.api);

  final ApiClient api;
  String state = 'not_started';
  String? lastError;
  String? token;
  String? permission;
  Map<String, dynamic>? registeredDevice;
  final _localNotifications = FlutterLocalNotificationsPlugin();
  final _openAlert = StreamController<String>.broadcast();

  /// Alert IDs from notification taps (foreground, background or cold start).
  Stream<String> get alertTaps => _openAlert.stream;

  static const _channel = AndroidNotificationChannel(
    'familypulse_alerts',
    'FamilyPulse alerts',
    description: 'Alerts about a family member you are connected with',
    importance: Importance.high,
  );

  Future<void> init() async {
    if (kIsWeb) {
      state = 'unsupported_on_web';
      notifyListeners();
      return;
    }
    try {
      await Firebase.initializeApp();
    } catch (e) {
      state = 'firebase_not_configured';
      lastError = e.toString();
      notifyListeners();
      return;
    }
    await _localNotifications.initialize(
      settings: const InitializationSettings(
        android: AndroidInitializationSettings('@mipmap/ic_launcher'),
        iOS: DarwinInitializationSettings(),
      ),
      onDidReceiveNotificationResponse: (r) {
        if (r.payload != null && r.payload!.isNotEmpty) {
          _openAlert.add(r.payload!);
        }
      },
    );
    await _localNotifications
        .resolvePlatformSpecificImplementation<
          AndroidFlutterLocalNotificationsPlugin
        >()
        ?.createNotificationChannel(_channel);

    FirebaseMessaging.onMessage.listen(_showForeground);
    FirebaseMessaging.onMessageOpenedApp.listen(_handleTap);
    final initial = await FirebaseMessaging.instance.getInitialMessage();
    if (initial != null) _handleTap(initial);
    FirebaseMessaging.instance.onTokenRefresh.listen((t) => _register(t));
    state = 'initialised';
    notifyListeners();
  }

  /// Ask permission and register this installation's token with the backend.
  Future<void> enable(String installationId) async {
    if (state == 'unsupported_on_web' || state == 'firebase_not_configured') {
      return;
    }
    _installationId = installationId;
    try {
      final settings = await FirebaseMessaging.instance.requestPermission();
      permission = settings.authorizationStatus.name;
      final t = await FirebaseMessaging.instance.getToken();
      if (t == null) {
        state = 'no_token';
      } else {
        await _register(t);
      }
    } catch (e) {
      state = 'error';
      lastError = e.toString();
    }
    notifyListeners();
  }

  String? _installationId;

  Future<void> _register(String t) async {
    token = t;
    final id = _installationId;
    if (id == null) return;
    final platform = defaultTargetPlatform == TargetPlatform.iOS
        ? 'ios'
        : 'android';
    registeredDevice = await api.registerPush(id, t, platform);
    state = 'registered';
    notifyListeners();
  }

  Future<Map<String, dynamic>> sendTest() async {
    final d = registeredDevice;
    if (d == null) throw StateError('No registered push device');
    return api.testPush(d['id'] as String);
  }

  void _handleTap(RemoteMessage m) {
    final id = m.data['alert_id'];
    if (id is String) _openAlert.add(id);
  }

  Future<void> _showForeground(RemoteMessage m) async {
    final n = m.notification;
    await _localNotifications.show(
      id: m.hashCode,
      title: n?.title ?? 'FamilyPulse',
      body: n?.body ?? 'A new alert is available.',
      notificationDetails: NotificationDetails(
        android: AndroidNotificationDetails(
          _channel.id,
          _channel.name,
          importance: Importance.high,
          priority: Priority.high,
        ),
        iOS: const DarwinNotificationDetails(),
      ),
      payload: m.data['alert_id'] as String?,
    );
  }
}
