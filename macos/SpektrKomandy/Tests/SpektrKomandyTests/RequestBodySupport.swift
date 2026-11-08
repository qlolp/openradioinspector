import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Darwin URLSession exposes POST bodies to URLProtocol as a stream, unlike Linux.
func bufferedTestRequest(_ request: URLRequest) throws -> URLRequest {
    guard request.httpBody == nil, let stream = request.httpBodyStream else { return request }
    stream.open()
    defer { stream.close() }
    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 4096)
    while true {
        let count = stream.read(&buffer, maxLength: buffer.count)
        guard count >= 0 else {
            throw stream.streamError ?? NSError(domain: "TestRequestBody", code: 1)
        }
        if count == 0 { break }
        data.append(contentsOf: buffer.prefix(count))
    }
    var copy = request
    copy.httpBody = data
    return copy
}
