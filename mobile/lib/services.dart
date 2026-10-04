import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:uuid/uuid.dart';

import 'api/api_client.dart';
import 'auth/auth_service.dart';
import 'collector/collector_bridge.dart';
import 'push/push_service.dart';

/// Simple service container (prototype). Injected in tests via [Services.test].
class Services {
  Services({required this.auth, required this.api, required this.push});

  factory Services.create() {
    final auth = AuthService();
    final api = ApiClient(auth);
    return Services(auth: auth, api: api, push: PushService(api));
  }

  final AuthService auth;
  final ApiClient api;
  final PushService push;
  final navigatorKey = GlobalKey<NavigatorState>();

  String? _installationId;

  /// Stable per-install id: from the Kotlin collector on Android, else secure storage.
  Future<String> installationId() async {
    if (_installationId != null) return _installationId!;
    if (CollectorBridge.isSupported) {
      return _installationId = await CollectorBridge.installationId();
    }
    const storage = FlutterSecureStorage();
    var id = await storage.read(key: 'installation_id');
    if (id == null) {
      id = 'i-${const Uuid().v4().replaceAll('-', '')}';
      await storage.write(key: 'installation_id', value: id);
    }
    return _installationId = id;
  }
}

late Services services;
