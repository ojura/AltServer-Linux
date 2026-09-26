#include "DevicePairingManager.h"

#include <iostream>
#include <memory>
#include <optional>
#include <string.h>
#include <netdb.h>
#include <pthread.h>
#include <exception>
#include <functional>

#include "ServerError.hpp"

// idevice.h also declares plist_* functions from idevice's own plist implementation, which clash with
// libplist's declarations, so this is the only file that includes it. The build prelinks idevice into
// a single object whose only global symbols are the functions listed in makefiles/idevice-build/idevice-api.txt.
extern "C" {
#include "ffi/idevice.h"
}

namespace
{
	std::string FFIErrorDescription(IdeviceFfiError* error)
	{
		if (error == NULL)
		{
			return "";
		}

		std::string description = "idevice error " + std::to_string(error->code);
		if (error->message != NULL)
		{
			description += ": " + std::string(error->message);
		}

		idevice_error_free(error);
		return description;
	}

	// Same messages as RemotePairingError in AltServer for macOS.
	ServerError PairingError(ServerErrorCode code, std::string failureReason, std::string recoverySuggestion, IdeviceFfiError* ffiError)
	{
		auto ffiDescription = FFIErrorDescription(ffiError);
		std::cout << "Failed to generate pairing file. " << failureReason;
		if (!ffiDescription.empty())
		{
			std::cout << " (" << ffiDescription << ")";
		}
		std::cout << std::endl;

		return ServerError(code, {
			{ "NSLocalizedFailureReason", failureReason },
			{ "NSLocalizedRecoverySuggestion", recoverySuggestion },
		});
	}

	ServerError UnknownPairingError(std::string reason, IdeviceFfiError* ffiError = NULL)
	{
		std::cout << "Remote AltServer setup failed: " << reason << std::endl;
		return PairingError(ServerErrorCode::Unknown,
			"Remote AltServer couldn't be configured for this device.",
			"Try again. If the problem continues, reconnect your device or restart your computer.",
			ffiError);
	}

	// Resolves the usbmuxd address the same way idevice_usbmuxd_new_default_connection() does
	// (USBMUXD_SOCKET_ADDRESS, then the default socket), so the device lookup and the pairing
	// connection go through the same usbmuxd. The macOS version always uses the default socket.
	UsbmuxdAddrHandle* CreateUsbmuxdAddress()
	{
		UsbmuxdAddrHandle* address = NULL;
		IdeviceFfiError* error = NULL;

		const char* socketAddress = getenv("USBMUXD_SOCKET_ADDRESS");
		if (socketAddress == NULL)
		{
			error = idevice_usbmuxd_default_addr_new(&address);
		}
		else if (strchr(socketAddress, ':') == NULL)
		{
			error = idevice_usbmuxd_unix_addr_new(socketAddress, &address);
		}
		else
		{
			// host:port, or [host]:port for IPv6.
			std::string value = socketAddress;
			auto separator = value.rfind(':');
			std::string host = value.substr(0, separator);
			std::string port = value.substr(separator + 1);
			if (host.size() >= 2 && host.front() == '[' && host.back() == ']')
			{
				host = host.substr(1, host.size() - 2);
			}

			struct addrinfo hints = {};
			hints.ai_flags = AI_NUMERICHOST | AI_NUMERICSERV;
			hints.ai_socktype = SOCK_STREAM;

			struct addrinfo* result = NULL;
			if (getaddrinfo(host.c_str(), port.c_str(), &hints, &result) != 0 || result == NULL)
			{
				throw UnknownPairingError("Invalid USBMUXD_SOCKET_ADDRESS: " + value);
			}

			error = idevice_usbmuxd_tcp_addr_new(result->ai_addr, result->ai_addrlen, &address);
			freeaddrinfo(result);
		}

		if (error != NULL)
		{
			throw UnknownPairingError("Couldn't create usbmuxd address.", error);
		}

		return address;
	}

	// Finds the usbmuxd device_id for this UDID.
	uint32_t ResolveDeviceID(std::string udid, UsbmuxdConnectionHandle* muxConnection)
	{
		UsbmuxdDeviceHandle** devices = NULL;
		int deviceCount = 0;
		if (auto error = idevice_usbmuxd_get_devices(muxConnection, &devices, &deviceCount))
		{
			throw UnknownPairingError("Couldn't query connected devices.", error);
		}

		std::optional<uint32_t> deviceID;
		for (int i = 0; i < deviceCount; i++)
		{
			char* deviceUDID = idevice_usbmuxd_device_get_udid(devices[i]);
			if (deviceUDID == NULL)
			{
				continue;
			}

			if (udid == deviceUDID)
			{
				deviceID = idevice_usbmuxd_device_get_device_id(devices[i]);
			}

			idevice_string_free(deviceUDID);

			if (deviceID.has_value())
			{
				break;
			}
		}

		idevice_usbmuxd_device_list_free(devices, deviceCount);

		if (!deviceID.has_value())
		{
			throw PairingError(ServerErrorCode::DeviceNotFound,
				"This device isn't connected to AltServer via USB.",
				"Connect your device to this computer with a cable, then try again.",
				NULL);
		}

		return *deviceID;
	}

	std::vector<unsigned char> PairAndSerialize(std::string udid, std::string hostName)
	{
		// 1. Connect to usbmuxd and resolve the device_id (required by idevice) for this UDID.
		UsbmuxdConnectionHandle* muxConnection = NULL;
		if (auto error = idevice_usbmuxd_new_default_connection(0, &muxConnection))
		{
			throw UnknownPairingError("Couldn't connect to usbmuxd.", error);
		}
		std::unique_ptr<UsbmuxdConnectionHandle, decltype(&idevice_usbmuxd_connection_free)> muxConnectionOwner(muxConnection, idevice_usbmuxd_connection_free);

		uint32_t deviceID = ResolveDeviceID(udid, muxConnection);

		// 2. Build a device provider. usbmuxd_provider_new() takes ownership of the address only when it succeeds.
		UsbmuxdAddrHandle* address = CreateUsbmuxdAddress();

		IdeviceProviderHandle* provider = NULL;
		if (auto error = usbmuxd_provider_new(address, 0, udid.c_str(), deviceID, hostName.c_str(), &provider))
		{
			idevice_usbmuxd_addr_free(address);
			throw UnknownPairingError("Couldn't create device provider.", error);
		}
		std::unique_ptr<IdeviceProviderHandle, decltype(&idevice_provider_free)> providerOwner(provider, idevice_provider_free);

		// 3. Pair. A NULL pin_callback makes idevice use "000000". Triggers the on-device "Trust" prompt.
		RpPairingFileHandle* pairingFile = NULL;
		if (auto error = tunnel_pair_usb(provider, hostName.c_str(), NULL, NULL, &pairingFile))
		{
			throw PairingError(ServerErrorCode::ConnectionFailed,
				"AltServer couldn't pair with this device.",
				"Make sure your device is unlocked, then tap Trust when prompted.",
				error);
		}
		std::unique_ptr<RpPairingFileHandle, decltype(&rp_pairing_file_free)> pairingFileOwner(pairingFile, rp_pairing_file_free);

		// 4. Serialize the plist.
		uint8_t* bytes = NULL;
		uintptr_t length = 0;
		if (auto error = rp_pairing_file_to_bytes(pairingFile, &bytes, &length))
		{
			throw UnknownPairingError("Couldn't serialize pairing file.", error);
		}

		if (bytes == NULL || length == 0)
		{
			throw UnknownPairingError("Pairing file serialized to empty data.");
		}

		std::vector<unsigned char> data(bytes, bytes + length);
		idevice_data_free(bytes, length);

		return data;
	}

	// Runs work on a new thread with an 8 MiB stack and returns its result or rethrows its exception.
	// tunnel_pair_usb() polls idevice's pairing future on the calling thread, and that poll needs more
	// than the 128 KiB that musl gives threads by default, including cpprestsdk's thread pool threads.
	// The kernel only backs the stack pages the thread touches.
	std::vector<unsigned char> RunOnLargeStack(std::function<std::vector<unsigned char>()> work)
	{
		struct Context
		{
			std::function<std::vector<unsigned char>()> work;
			std::vector<unsigned char> result;
			std::exception_ptr exception;
		} context = { work, {}, nullptr };

		auto threadMain = [](void* argument) -> void* {
			auto context = static_cast<Context*>(argument);
			try
			{
				context->result = context->work();
			}
			catch (...)
			{
				context->exception = std::current_exception();
			}
			return NULL;
		};

		pthread_attr_t attributes;
		pthread_attr_init(&attributes);
		pthread_attr_setstacksize(&attributes, 8 * 1024 * 1024);

		pthread_t thread;
		int result = pthread_create(&thread, &attributes, threadMain, &context);
		pthread_attr_destroy(&attributes);

		if (result != 0)
		{
			throw UnknownPairingError("Couldn't start pairing thread: " + std::string(strerror(result)));
		}

		pthread_join(thread, NULL);

		if (context.exception)
		{
			std::rethrow_exception(context.exception);
		}

		return context.result;
	}
}

DevicePairingManager* DevicePairingManager::instance()
{
	static DevicePairingManager instance;
	return &instance;
}

std::vector<unsigned char> DevicePairingManager::GeneratePairingFile(std::string udid, std::string hostName)
{
	return RunOnLargeStack([udid, hostName]() { return PairAndSerialize(udid, hostName); });
}
