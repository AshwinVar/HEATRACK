import 'package:flutter/material.dart';

import '../config.dart';
import '../services.dart';
import '../widgets/common.dart';

class SignInScreen extends StatefulWidget {
  const SignInScreen({super.key});

  @override
  State<SignInScreen> createState() => _SignInScreenState();
}

class _SignInScreenState extends State<SignInScreen> {
  final _email = TextEditingController();
  final _password = TextEditingController();
  bool _busy = false;

  Future<void> _go({bool signUp = false}) async {
    setState(() => _busy = true);
    try {
      await services.auth.signIn(
        _email.text.trim(),
        _password.text,
        signUp: signUp,
      );
    } catch (e) {
      if (mounted) await showError(context, e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final dev = AppConfig.isDevAuth;
    return Scaffold(
      appBar: AppBar(title: const Text('FamilyPulse')),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          if (dev)
            Container(
              color: Colors.amber.shade200,
              padding: const EdgeInsets.all(12),
              child: const Text(
                'DEVELOPMENT SIGN-IN: tokens are minted by a local development backend. '
                'Not available against a production server.',
                key: Key('dev-auth-banner'),
                style: TextStyle(fontWeight: FontWeight.bold),
              ),
            ),
          const SizedBox(height: 16),
          const Text(
            'Share selected wellness readings with family. Not an emergency service and not '
            'a medical device.',
            style: TextStyle(fontSize: 18),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _email,
            keyboardType: TextInputType.emailAddress,
            decoration: const InputDecoration(
              labelText: 'Email',
              border: OutlineInputBorder(),
            ),
          ),
          if (!dev) ...[
            const SizedBox(height: 12),
            TextField(
              controller: _password,
              obscureText: true,
              decoration: const InputDecoration(
                labelText: 'Password',
                border: OutlineInputBorder(),
              ),
            ),
          ],
          const SizedBox(height: 16),
          BigButton(
            label: 'Sign in',
            icon: Icons.login,
            onPressed: _busy ? null : _go,
          ),
          if (!dev)
            TextButton(
              onPressed: _busy ? null : () => _go(signUp: true),
              child: const Text(
                'Create account',
                style: TextStyle(fontSize: 18),
              ),
            ),
        ],
      ),
    );
  }
}
