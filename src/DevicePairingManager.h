#pragma once

#include <string>
#include <vector>

// Generates RemotePairing (RPPairing) files over a wired connection using idevice
// (https://github.com/jkcoxson/idevice), like DevicePairingManager.swift in AltServer for macOS.
// AltStore uses the pairing file to install and refresh apps on-device through LocalDevVPN.
class DevicePairingManager
{
public:
	static DevicePairingManager* instance();

	// Pairs with the USB-connected device identified by udid. The device shows a Trust prompt,
	// and hostName is the name it lists under Settings > General > VPN & Device Management.
	// Returns the serialized pairing file (a property list). Throws ServerError on failure.
	std::vector<unsigned char> GeneratePairingFile(std::string udid, std::string hostName);

private:
	DevicePairingManager() = default;
};
